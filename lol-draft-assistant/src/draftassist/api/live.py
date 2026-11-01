"""Live screen: champ select session -> view payload -> WebSocket push to every open screen.

Every session event is parsed and scored from scratch; nothing is cached across events, so a
champion locked a moment ago is gone from the next push.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect

from draftassist.api.draftlog import DraftLogger
from draftassist.data.provider import ChampionCatalog
from draftassist.engine.engine import DraftEngine, Suggestion
from draftassist.engine.guards import strict_mode
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.state import DraftState
from draftassist.lcu.parser import LiveDraft, SessionParseError, parse_session
from draftassist.lcu.recorder import SessionRecorder

log = logging.getLogger(__name__)

class PlayerDirectory(Protocol):
    """Our players: PUUID -> role for the parser, and filling PUUIDs from champ select teammates."""

    def roles_by_puuid(self) -> dict[str, str]: ...

    def resolve_from_session(self, members: list[dict]) -> int: ...


HOVER_FLAG_RISK = 0.2  # log-odds; a hover whose best open counter wins its lane ~55%+ gets flagged
IDLE_PHASES = {"None", "Lobby", "Matchmaking", "ReadyCheck"}  # gameflow phases that clear the screen


class LiveHub:
    """Holds the latest payload and pushes each new one to every connected screen."""

    def __init__(self) -> None:
        self.latest: dict = {"status": "waiting", "message": "Starting up"}
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def publish(self, payload: dict) -> None:
        self.latest = payload
        for ws in list(self._clients):
            try:
                await ws.send_json(payload)
            except Exception:
                self._clients.discard(ws)

    def publish_threadsafe(self, payload: dict) -> None:
        if self._loop is None or self._loop.is_closed():
            self.latest = payload
            return
        asyncio.run_coroutine_threadsafe(self.publish(payload), self._loop)

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        try:
            await ws.send_json(self.latest)
            while True:
                await ws.receive_text()  # screens don't send anything; this detects disconnects
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(ws)


def _ban_card(catalog: ChampionCatalog, s: Suggestion, hovered: set[int], weights=None) -> dict:
    card = {"champ": catalog.card(s.champ_id), "reason": s.reason, "value": round(s.value, 4),
            "relevance": None if s.relevance is None else round(s.relevance, 3),
            "discount": None if s.discount is None else round(s.discount, 3),
            "warnings": list(s.warnings), "highlighted": s.champ_id in hovered}
    if s.threat is not None and weights is not None:
        # Each component in log-odds, unweighted and as it enters the ban value (weighted).
        card["components"] = {
            name: {"raw": round(raw, 5), "weighted": round(weight * raw, 5)}
            for name, raw, weight in (("threat", s.threat, weights.threat), ("matchup", s.matchup, weights.matchup),
                                      ("comp", s.comp, weights.comp))}
    return card


NO_USEFUL_BAN = "no useful ban \u2014 free choice"


def _ban_group(engine: DraftEngine, catalog: ChampionCatalog, live: LiveDraft, hovered: set[int]) -> dict:
    """Simultaneous bans: one card per teammate with our coordinated suggestion and their status."""
    st = live.state
    open_bans = [b for b in live.ally_bans if b.status in ("pending", "hovering")]
    teammates_hovering = {b.champ_id for b in open_bans if b.status == "hovering"}
    chosen = engine.ban_group(st.with_provisional(teammates_hovering), [b.role for b in open_bans])
    suggestion_for = {id(b): s for b, (_, s) in zip(open_bans, chosen)}
    order = {r: i for i, r in enumerate(ROLES)}
    cards = []
    for b in sorted(live.ally_bans, key=lambda b: (order.get(b.role or "", 9), b.cell_id)):
        s = suggestion_for.get(id(b))
        cards.append({
            "role": b.role, "role_label": ROLE_LABELS.get(b.role or "", "Teammate"), "player": b.player,
            "status": "suggested" if b.status == "pending" else b.status,
            "status_champ": catalog.card(b.champ_id) if b.status in ("hovering", "locked") else None,
            "suggestion": _ban_card(catalog, s, hovered, engine.cfg.ban_weights) if s else None,
            "note": None if s or b.status in ("locked", "skipped") else NO_USEFUL_BAN,
        })
    return {"mode": "our_ban_group", "title": "BANS", "cards": cards}


def _pick_card(catalog: ChampionCatalog, s: Suggestion, hovered: set[int]) -> dict:
    warnings = []
    if s.champ_id in hovered and s.counter_risk >= HOVER_FLAG_RISK and s.counter_id:
        warnings.append(f"⚠ easy to counter: {catalog.card(s.counter_id)['name']}")
    return {"champ": catalog.card(s.champ_id), "reason": s.reason, "role": s.role,
            "role_label": ROLE_LABELS.get(s.role or "", ""), "win_prob": round(s.win_prob, 4),
            "warnings": warnings, "highlighted": s.champ_id in hovered}


def _score_hover(engine: DraftEngine, state: DraftState, open_roles: list[str], champ: int,
                 role: str | None) -> Suggestion | None:
    """Our engine's view of a teammate's hover, wherever it ranks."""
    ranked = engine.suggest_picks(state, k=10_000, role=role if role in open_roles else None)
    return next((s for s in ranked if s.champ_id == champ), None)


def build_live_view(engine: DraftEngine, catalog: ChampionCatalog, live: LiveDraft,
                    data_source: str, note: str | None) -> dict:
    st = live.state
    ev = engine.evaluate(st)
    card = catalog.card
    step = st.step
    finished = step is None or live.timer_phase in ("FINALIZATION", "GAME_STARTING")
    if live.timer_phase == "PLANNING":
        phase = "Planning"
    elif finished:
        phase = "Locked in"
    else:
        phase = step.label
    turn = None if finished else ("us" if st.is_our_turn else "them")
    open_roles = [r for r in ROLES if r not in ev.our_roles.values()]
    hovered = {c for c, _ in st.hovers}

    our_slots = []
    role_order = {r: i for i, r in enumerate(ROLES)}
    for s in sorted(live.our_slots, key=lambda s: (role_order.get(s.role or ev.our_roles.get(s.champ_id, ""), 9), s.cell_id)):
        role = s.role or ev.our_roles.get(s.champ_id)
        slot = {"role": role, "role_label": ROLE_LABELS.get(role or "", "?"), "champ": card(s.champ_id),
                "hover": card(s.hover_id), "is_local": s.is_local, "hover_score": None}
        if s.hover_id and s.hover_id not in st.unavailable() and not finished:
            scored = _score_hover(engine, st, open_roles, s.hover_id, s.role)
            if scored is not None:
                flag = (f"⚠ easy to counter: {card(scored.counter_id)['name']}"
                        if scored.counter_risk >= HOVER_FLAG_RISK and scored.counter_id else None)
                slot["hover_score"] = {"win_prob": round(scored.win_prob, 4), "reason": scored.reason, "warning": flag}
        our_slots.append(slot)
    their_slots = [{"champ": card(s.champ_id), "role": ev.their_roles.get(s.champ_id),
                    "role_label": ROLE_LABELS.get(ev.their_roles.get(s.champ_id, ""), "")}
                   for s in live.their_slots]

    center: dict[str, Any]
    if finished:
        center = {"mode": "locked", "win_prob": round(ev.win_prob, 4),
                  "threats": [{"champ": card(t.champ_id), "reason": t.reason} for t in engine.threats(st, k=2)]}
    elif st.format.simultaneous_bans and any(b.status in ("pending", "hovering") for b in live.ally_bans):
        center = _ban_group(engine, catalog, live, hovered)
    elif live.ally_action == "ban" or (st.is_our_turn and step.kind == "ban"):
        # Ranked bans are simultaneous: show ours while any teammate is still banning, minus the
        # champions teammates are already about to ban.
        about_to_ban = live.ally_ban_hovers
        bans = [s for s in engine.suggest_bans(st, k=3 + len(about_to_ban)) if s.champ_id not in about_to_ban][:3]
        center = {"mode": "our_ban", "title": "BAN",
                  "cards": [_ban_card(catalog, s, hovered, engine.cfg.ban_weights) for s in bans]}
    elif st.is_our_turn:
        role = live.actor_role if live.actor_role in open_roles else None
        picks = engine.suggest_picks(st, k=3, role=role)
        cards = [_pick_card(catalog, s, hovered) for s in picks]
        center = {"mode": "our_pick", "title": f"PICK — {ROLE_LABELS[role]}" if role else "PICK", "cards": cards,
                  "hover_card": None}
        actor_hover = next((sl.hover_id for sl in live.our_slots if sl.cell_id == live.actor_cell_id), 0)
        if actor_hover and actor_hover not in {s.champ_id for s in picks}:
            scored = _score_hover(engine, st, open_roles, actor_hover, live.actor_role)
            if scored is not None:
                center["hover_card"] = _pick_card(catalog, scored, hovered)
    else:
        tables = engine.lane_ban_tables(st)
        cards = []
        for s in engine.likely_enemy_picks(st, k=3):
            row = tables[s.role].row(s.champ_id) if s.role else None
            answer = None
            if row is not None and row.via is not None:
                answer = {"champ": card(row.via), "resp_pp": round(row.resp, 1)}
            cards.append({"champ": card(s.champ_id), "prob": round(s.enemy_prob, 4), "role": s.role,
                          "role_label": ROLE_LABELS.get(s.role or "", ""), "answer": answer,
                          "answer_note": None if answer else (row.via_note if row else "no answer in our pool")})
        center = {"mode": "enemy_turn", "title": f"THEIR {'BAN' if step.kind == 'ban' else 'PICK'}", "cards": cards}

    return {
        "status": "champ_select", "data_source": data_source, "note": note,
        "format": st.format.name, "side": st.our_side, "phase": phase, "turn": turn,
        "action": None if finished else step.kind, "seconds_left": live.seconds_left, "game_id": live.game_id,
        "our_slots": our_slots, "their_slots": their_slots,
        "our_bans": [card(c) for c in st.bans(st.our_side)],
        "their_bans": [card(c) for c in st.bans(st.their_side)],
        "center": center,
    }


class LiveService:
    """Turns client events into Live screen payloads and draft logs. Safe to call from the
    connector thread."""

    def __init__(self, engine: DraftEngine, catalog: ChampionCatalog, hub: LiveHub, data_source: str,
                 note: str | None, draft_format: str = "auto", recorder: SessionRecorder | None = None,
                 logger: DraftLogger | None = None, players: PlayerDirectory | None = None,
                 on_game_logged: Callable[[int], None] | None = None):
        self.engine, self.catalog, self.hub = engine, catalog, hub
        self.data_source, self.note, self.draft_format, self.recorder = data_source, note, draft_format, recorder
        self.logger, self.players, self.on_game_logged = logger, players, on_game_logged
        self.client_status = "starting"
        self.gameflow_phase: str | None = None
        self.queue_id: int | None = None
        self._resolved_for: int | None = None  # game id whose teammates were already matched to players

    def _waiting(self) -> dict:
        return {"status": "waiting", "message": "Waiting for champ select", "client": self.client_status,
                "gameflow_phase": self.gameflow_phase, "data_source": self.data_source, "note": self.note}

    def _build(self, session: dict | None) -> tuple[dict, LiveDraft | None]:
        if session is None:
            return self._waiting(), None
        try:
            roles = self.players.roles_by_puuid() if self.players else None
            live = parse_session(session, self.draft_format, roles)
            return build_live_view(self.engine, self.catalog, live, self.data_source, self.note), live
        except SessionParseError as e:
            log.error("could not parse champ select session: %s", e)
            return {"status": "error", "message": str(e)}, None
        except Exception:
            if strict_mode():
                raise
            log.exception("live view failed")
            return {"status": "error", "message": "Suggestion error (see server log)"}, None

    def view(self, session: dict | None) -> dict:
        return self._build(session)[0]

    def handle_session(self, session: dict | None) -> dict:
        if self.recorder is not None:
            if session is None:
                self.recorder.end()
            else:
                self.recorder.record("session", session)
        latest = self.hub.latest
        if session is None and latest.get("status") == "champ_select" and latest.get("phase") == "Locked in":
            return latest  # game is loading: keep win % and threats up until the client goes idle
        payload, live = self._build(session)
        self.hub.publish_threadsafe(payload)
        if live is not None:
            if self.players is not None and self._resolved_for != (live.game_id or -1):
                self._resolved_for = live.game_id or -1
                self.players.resolve_from_session(session.get("myTeam") or [])
            if self.logger is not None:
                self.logger.observe(live, payload)
        return payload

    def handle_phase(self, phase: str, gameflow_session: dict | None) -> None:
        self.gameflow_phase = phase
        if gameflow_session:
            self.queue_id = (gameflow_session.get("gameData") or {}).get("queue", {}).get("id")
            if self.recorder is not None and phase == "ChampSelect":
                self.recorder.record("gameflow", gameflow_session)
        if self.logger is not None:
            self.logger.set_queue(self.queue_id)
            self.logger.on_phase(phase)
        if phase in IDLE_PHASES:
            self.hub.publish_threadsafe(self._waiting())

    def handle_end_of_game(self, eog: dict | None) -> int | None:
        if self.recorder is not None and eog:
            self.recorder.record("eog", eog)
        if self.logger is None:
            return None
        draft_id = self.logger.on_end_of_game(eog)
        if draft_id is not None and self.on_game_logged is not None:
            self.on_game_logged(draft_id)
        return draft_id

    def handle_status(self, status: str) -> None:
        self.client_status = status
        if self.hub.latest.get("status") != "champ_select":
            self.hub.publish_threadsafe(self._waiting())
