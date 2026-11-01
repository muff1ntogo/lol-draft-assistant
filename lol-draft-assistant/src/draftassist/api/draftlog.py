"""Auto-logging of every draft and its result (spec, Core workflows: live draft and post-game).

During champ select every event is observed: for each step we keep what the screen showed while
that step was in progress (our top 3 on our turns, our predicted picks on theirs). When the game
starts the draft is written to `drafts` / `draft_actions`, with each action's rank among what we
showed. A champ select that never reaches the game (a dodge) is dropped. The result comes from the
client's end-of-game stats (works for customs too); match-v5 confirms it later (data/postgame.py).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import Engine
from sqlmodel import Session, delete, select

from draftassist.db.models import Draft, DraftAction
from draftassist.engine.state import SKIPPED, DraftState
from draftassist.lcu.parser import LiveDraft

log = logging.getLogger(__name__)

GAME_STARTED = {"GameStart", "InProgress", "WaitingForStats", "PreEndOfGame", "EndOfGame", "Reconnect"}
DRAFT_OVER = {"None", "Lobby", "Matchmaking", "ReadyCheck"}  # back here without a game: a dodge
REMAKE_SECONDS = 300


@dataclass
class _Pending:
    game_id: int | None
    side: str
    draft_format: str
    started_at: datetime
    queue_id: int | None = None
    actions: tuple[int, ...] = ()
    shown: dict[int, dict] = field(default_factory=dict)  # step index -> {"kind", "items": [...]}
    pred_win_prob: float | None = None
    draft_id: int | None = None


def _snapshot(center: dict) -> dict | None:
    mode = center.get("mode")
    if mode in ("our_ban", "our_pick"):
        return {"kind": "suggested", "items": [
            {"champ_id": c["champ"]["id"], "name": c["champ"]["name"], "reason": c["reason"]} for c in center["cards"]]}
    if mode == "enemy_turn":
        return {"kind": "predicted", "items": [
            {"champ_id": c["champ"]["id"], "name": c["champ"]["name"], "prob": c["prob"]} for c in center["cards"]]}
    return None


def _open_steps(st: DraftState, mode: str | None) -> list[int]:
    """Steps the current screen is about: every step of the current turn that isn't locked yet.
    Our bans in ranked are simultaneous, so that's all of our remaining bans in this phase."""
    steps, n = st.format.steps, len(st.actions)
    if mode == "our_ban":
        phase = st.step.phase
        return [s.index for s in steps if s.side == st.our_side and s.kind == "ban" and s.phase == phase
                and s.index >= n]
    first = st.step
    out = []
    for s in steps[first.index:]:
        if s.side != first.side or s.kind != first.kind:
            break
        out.append(s.index)
    return out


def parse_eog(eog: dict | None) -> tuple[int | None, str | None, str | None]:
    """End-of-game stats block -> (game_id, result, side). Fields are read defensively."""
    if not eog:
        return None, None, None
    mine = next((t for t in eog.get("teams") or [] if t.get("isPlayerTeam")), None)
    side = {100: "blue", 200: "red"}.get(mine.get("teamId")) if mine else None
    if eog.get("gameLength") is not None and eog["gameLength"] < REMAKE_SECONDS:
        result = "remake"
    elif mine is None or "isWinningTeam" not in mine:
        result = None
    else:
        result = "win" if mine["isWinningTeam"] else "loss"
    return eog.get("gameId") or None, result, side


class DraftLogger:
    def __init__(self, db: Engine, clock=lambda: datetime.now(timezone.utc)):
        self.db = db
        self._clock = clock
        self.pending: _Pending | None = None

    # ---------- champ select ----------
    def observe(self, live: LiveDraft, payload: dict) -> None:
        st = live.state
        p = self.pending
        # Same draft as long as nothing locked disappears: the two picks of a turn may lock in
        # either order, and hidden ranked bans are revealed later.
        new_draft = (p is None or (live.game_id and p.game_id and live.game_id != p.game_id)
                     or len(st.actions) < len(p.actions)
                     or not {c for c in p.actions if c != SKIPPED} <= set(st.actions))
        if new_draft:
            p = self.pending = _Pending(live.game_id, st.our_side, st.format.name, self._clock())
        if live.game_id and not p.game_id:
            p.game_id = live.game_id
        center = payload.get("center") or {}
        if st.step is not None and center.get("mode") != "locked":
            snap = _snapshot(center)
            if snap is not None:
                for i in _open_steps(st, center.get("mode")):
                    p.shown[i] = snap
        if center.get("mode") == "locked":
            p.pred_win_prob = center.get("win_prob")
        p.actions = st.actions
        if p.draft_id is not None:
            self._write(p)

    def set_queue(self, queue_id: int | None) -> None:
        if self.pending is not None and queue_id is not None:
            self.pending.queue_id = queue_id

    # ---------- gameflow ----------
    def on_phase(self, phase: str) -> int | None:
        p = self.pending
        if p is None:
            return None
        if phase in GAME_STARTED and p.draft_id is None and p.actions:
            self._write(p)
            log.info("logged draft %s (game %s, %d actions)", p.draft_id, p.game_id, len(p.actions))
        elif phase in DRAFT_OVER and p.draft_id is None:
            log.info("champ select ended without a game; draft not logged")
            self.pending = None
        return p.draft_id

    def on_end_of_game(self, eog: dict | None) -> int | None:
        """Record the result. Returns the draft id it was written to (None if no draft matched)."""
        game_id, result, side = parse_eog(eog)
        p = self.pending
        if p is not None and p.draft_id is None and p.actions:
            self._write(p)  # the app missed the game-start phase
        with Session(self.db) as s:
            draft = None
            if game_id is not None:
                draft = s.exec(select(Draft).where(Draft.game_id == game_id)).first()
            if draft is None and p is not None and p.draft_id is not None:
                draft = s.get(Draft, p.draft_id)
            if draft is None:
                log.warning("end of game %s: no logged draft to attach the result to", game_id)
                return None
            if game_id and not draft.game_id:
                draft.game_id = game_id
            if result:
                draft.result, draft.result_source = result, "lcu"
            if side and side != draft.side:
                log.warning("draft %s: end-of-game says side %s, champ select said %s", draft.draft_id, side, draft.side)
            s.add(draft)
            s.commit()
            draft_id = draft.draft_id
        self.pending = None
        return draft_id

    # ---------- persistence ----------
    def _write(self, p: _Pending) -> None:
        from draftassist.engine.formats import get_format

        steps = get_format(p.draft_format).steps
        with Session(self.db) as s:
            draft = s.get(Draft, p.draft_id) if p.draft_id else None
            if draft is None and p.game_id:
                draft = s.exec(select(Draft).where(Draft.game_id == p.game_id)).first()
            if draft is None:
                draft = Draft(game_id=p.game_id, side=p.side, played_at=p.started_at)
            draft.queue_id = p.queue_id if p.queue_id is not None else draft.queue_id
            draft.draft_format = p.draft_format
            draft.pred_win_prob = p.pred_win_prob if p.pred_win_prob is not None else draft.pred_win_prob
            s.add(draft)
            s.flush()
            p.draft_id = draft.draft_id
            s.exec(delete(DraftAction).where(DraftAction.draft_id == draft.draft_id))
            for i, champ in enumerate(p.actions):
                shown = p.shown.get(i)
                ids = [item["champ_id"] for item in shown["items"]] if shown else []
                s.add(DraftAction(draft_id=draft.draft_id, seq=i + 1, side=steps[i].side, type=steps[i].kind,
                                  champ_id=champ, our_rank=ids.index(champ) + 1 if champ in ids else None,
                                  suggestions_json=json.dumps(shown) if shown else None))
            s.commit()
