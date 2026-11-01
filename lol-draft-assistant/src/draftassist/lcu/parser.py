"""Champ select session JSON (`/lol-champ-select/v1/session`) -> DraftState + display data.

All knowledge of LCU field names lives here. Client patches rename fields, so missing fields
raise SessionParseError naming the field instead of failing silently.

Mapping onto the configured draft order: completed ban/pick actions are queued per (side, kind)
in action-id order, then the format's steps are filled in order. In tournament draft this is
exactly the completion order; for standard ranked's simultaneous bans it puts each side's bans
in its own ban steps.
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from draftassist.engine.formats import DraftFormat, Side, get_format
from draftassist.engine.state import SKIPPED, DraftState

# LCU assignedPosition -> engine role
POSITIONS = {"top": "top", "jungle": "jungle", "middle": "mid", "bottom": "adc", "utility": "support"}
TEAM_SIDE: dict[int, Side] = {1: "blue", 2: "red"}


class SessionParseError(ValueError):
    pass


def _req(obj: dict, key: str, where: str) -> Any:
    try:
        return obj[key]
    except (KeyError, TypeError):
        raise SessionParseError(f"champ select session: missing field {where}.{key}") from None


@dataclass(frozen=True)
class Slot:
    cell_id: int
    role: str | None  # engine role from assignedPosition; None when the client doesn't assign one
    champ_id: int  # locked champion, 0 if none
    hover_id: int  # pick intent / hovered champion, 0 if none
    is_local: bool = False
    puuid: str | None = None
    riot_id: str | None = None  # "gameName#tagLine" when the client exposes it


@dataclass(frozen=True)
class LiveDraft:
    state: DraftState
    our_slots: tuple[Slot, ...]
    their_slots: tuple[Slot, ...]
    timer_phase: str  # PLANNING | BAN_PICK | FINALIZATION | GAME_STARTING
    seconds_left: float | None
    actor_cell_id: int | None  # whose action is in progress
    actor_role: str | None  # our player's role when it's our pick
    game_id: int | None


def detect_format(session: dict) -> DraftFormat:
    """Tournament draft bans one at a time; standard ranked bans in one simultaneous turn."""
    for turn in _req(session, "actions", "session"):
        bans = [a for a in turn if _req(a, "type", "actions[]") == "ban"]
        if len(bans) > 1:
            return get_format("ranked")
    return get_format("tournament")


def parse_session(session: dict, format_name: str = "auto",
                  roles_by_puuid: Mapping[str, str] | None = None) -> LiveDraft:
    """`roles_by_puuid` (our players table) fills in roles when the client assigns no position."""
    fmt = detect_format(session) if format_name == "auto" else get_format(format_name)
    known_roles = roles_by_puuid or {}
    my_team = _req(session, "myTeam", "session")
    their_team = _req(session, "theirTeam", "session")
    local_cell = _req(session, "localPlayerCellId", "session")
    if not my_team:
        raise SessionParseError("champ select session: myTeam is empty")
    our_side = TEAM_SIDE.get(_req(my_team[0], "team", "myTeam[]"))
    if our_side is None:
        raise SessionParseError(f"champ select session: unknown team id {my_team[0].get('team')!r}")
    their_side: Side = "red" if our_side == "blue" else "blue"

    actions = [a for turn in _req(session, "actions", "session") for a in turn]
    actions = [a for a in actions if _req(a, "type", "actions[]") in ("ban", "pick")]
    actions.sort(key=lambda a: _req(a, "id", "actions[]"))

    completed: dict[tuple[str, str], deque[int]] = defaultdict(deque)
    pending_picks: dict[int, int] = {}  # actor cell -> hovered champion (incomplete pick action)
    locked: dict[int, int] = {}  # actor cell -> locked champion (completed pick action)
    actor_cell = None
    for a in actions:
        side = our_side if _req(a, "isAllyAction", "actions[]") else their_side
        champ = _req(a, "championId", "actions[]")
        if _req(a, "completed", "actions[]"):
            completed[(side, a["type"])].append(champ)
            if a["type"] == "pick":
                locked[_req(a, "actorCellId", "actions[]")] = champ
        else:
            if a.get("isInProgress"):
                actor_cell = _req(a, "actorCellId", "actions[]")
            if a["type"] == "pick" and side == our_side and champ:
                pending_picks[_req(a, "actorCellId", "actions[]")] = champ

    steps: list[int] = []
    for step in fmt.steps:
        queue = completed.get((step.side, step.kind))
        if not queue:
            break
        steps.append(queue.popleft())

    def slots(team: list[dict], ours: bool) -> tuple[Slot, ...]:
        out = []
        for m in team:
            cell = _req(m, "cellId", "team[]")
            selected = _req(m, "championId", "team[]")  # current selection: hovered OR locked
            champ = locked.get(cell, 0)
            hover = 0
            if ours and not champ:
                hover = pending_picks.get(cell) or selected or m.get("championPickIntent", 0) or 0
            puuid = m.get("puuid") or None
            role = POSITIONS.get(m.get("assignedPosition") or "") or (known_roles.get(puuid) if ours else None)
            riot_id = f"{m['gameName']}#{m.get('tagLine', '')}" if m.get("gameName") else None
            out.append(Slot(cell, role, champ, hover, cell == local_cell, puuid, riot_id))
        return tuple(out)

    our_slots, their_slots = slots(my_team, True), slots(their_team, False)
    # A hover is provisional: never a champion that is already banned or picked.
    taken = {c for c in steps if c != SKIPPED}
    hovers = tuple((s.hover_id, s.role or "") for s in our_slots if s.hover_id and s.hover_id not in taken)
    state = DraftState(fmt, our_side, tuple(steps), hovers)

    timer = _req(session, "timer", "session")
    left = timer.get("adjustedTimeLeftInPhase")
    actor_role = next((s.role for s in our_slots if s.cell_id == actor_cell), None)
    return LiveDraft(
        state=state,
        our_slots=our_slots,
        their_slots=their_slots,
        timer_phase=_req(timer, "phase", "timer"),
        seconds_left=None if timer.get("isInfinite") or left is None else max(left, 0) / 1000,
        actor_cell_id=actor_cell,
        actor_role=actor_role,
        game_id=session.get("gameId") or None,
    )
