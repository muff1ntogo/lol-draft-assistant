"""Champ select session JSON (`/lol-champ-select/v1/session`) -> DraftState + display data.

All knowledge of LCU field names lives here. Client patches rename fields, so missing fields
raise SessionParseError naming the field instead of failing silently.

Mapping onto the configured draft order: completed ban/pick actions are queued per (side, kind)
in action-id order, then the format's steps are filled in order. In tournament draft this is
exactly the completion order; for standard ranked's simultaneous bans it puts each side's bans
in its own ban steps.

Verified against a recorded Ranked Flex champ select (fixtures/lcu/flex-ranked-sample):
- a skipped ban comes through as championId -1 (0 means "not chosen yet");
- ranked bans are one simultaneous turn and the enemy's stay hidden until `ten_bans_reveal`, so
  hidden enemy bans hold their place as skipped until revealed, letting our own completed bans
  count straight away;
- the two picks of one turn can lock in either order.
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
TOURNAMENT_BANS_BEFORE_PICKS = 6  # standard ranked has 10


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
class AllyBan:
    """One of our players' ban actions (standard ranked: all five happen at once)."""
    cell_id: int
    role: str | None
    player: str | None  # in-game name, when the client exposes it
    status: str  # pending | hovering | locked | skipped
    champ_id: int  # hovered or locked champion, 0 otherwise


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
    ally_action: str | None = None  # "ban" | "pick" while one of our actions is in progress
    ally_ban_hovers: frozenset[int] = frozenset()  # champions teammates are about to ban
    ally_bans: tuple[AllyBan, ...] = ()  # our ban actions in a simultaneous ban phase


def detect_format(session: dict) -> DraftFormat:
    """Standard ranked (Solo/Duo, Flex) has all 10 bans before the first pick; tournament draft
    (Ranked 5s, Clash) has 6. Counted in action-id order, so it doesn't depend on how the client
    groups actions into turns. Without any pick actions listed, simultaneous bans mean ranked."""
    turns = _req(session, "actions", "session")
    actions = sorted((a for turn in turns for a in turn if _req(a, "type", "actions[]") in ("ban", "pick")),
                     key=lambda a: _req(a, "id", "actions[]"))
    first_pick = next((i for i, a in enumerate(actions) if a["type"] == "pick"), None)
    if first_pick is not None:
        return get_format("ranked" if first_pick > TOURNAMENT_BANS_BEFORE_PICKS else "tournament")
    simultaneous = any(sum(1 for a in turn if a.get("type") == "ban") > 1 for turn in turns)
    return get_format("ranked" if simultaneous else "tournament")


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
    ally_cell = enemy_cell = ally_action = None
    ally_ban_hovers: set[int] = set()
    ally_ban_actions: list[tuple[int, str, int]] = []  # (actor cell, status, champion)
    for a in actions:
        ally = _req(a, "isAllyAction", "actions[]")
        side = our_side if ally else their_side
        champ = _req(a, "championId", "actions[]")
        if ally and a["type"] == "ban" and fmt.simultaneous_bans:
            done = _req(a, "completed", "actions[]")
            status = ("locked" if champ > 0 else "skipped") if done else ("hovering" if champ > 0 else "pending")
            ally_ban_actions.append((_req(a, "actorCellId", "actions[]"), status, max(champ, 0)))
        if _req(a, "completed", "actions[]"):
            if a["type"] == "ban":
                completed[(side, "ban")].append(champ if champ > 0 else SKIPPED)  # -1 = skipped
            elif champ > 0:
                completed[(side, "pick")].append(champ)
                locked[_req(a, "actorCellId", "actions[]")] = champ
        else:
            if a.get("isInProgress"):
                cell = _req(a, "actorCellId", "actions[]")
                if ally and ally_cell is None:
                    ally_cell, ally_action = cell, a["type"]
                elif not ally and enemy_cell is None:
                    enemy_cell = cell
                if ally and a["type"] == "ban" and champ > 0:
                    ally_ban_hovers.add(champ)
            if a["type"] == "pick" and side == our_side and champ > 0:
                pending_picks[_req(a, "actorCellId", "actions[]")] = champ
    actor_cell = ally_cell if ally_cell is not None else enemy_cell

    steps: list[int] = []
    for i, step in enumerate(fmt.steps):
        queue = completed.get((step.side, step.kind))
        if queue:
            steps.append(queue.popleft())
        elif step.kind == "ban" and fmt.name == "ranked" and any(
                completed.get((s.side, "ban")) for s in fmt.steps[i + 1:] if s.kind == "ban"):
            steps.append(SKIPPED)  # simultaneous bans: the other side's aren't revealed yet
        else:
            break

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
    by_cell = {s.cell_id: s for s in our_slots}
    ally_bans = tuple(
        AllyBan(cell, by_cell[cell].role if cell in by_cell else None,
                (by_cell[cell].riot_id or "").split("#")[0] or None if cell in by_cell else None, status, champ)
        for cell, status, champ in ally_ban_actions)
    return LiveDraft(
        state=state,
        our_slots=our_slots,
        their_slots=their_slots,
        timer_phase=_req(timer, "phase", "timer"),
        seconds_left=None if timer.get("isInfinite") or left is None else max(left, 0) / 1000,
        actor_cell_id=actor_cell,
        actor_role=actor_role,
        game_id=session.get("gameId") or None,
        ally_action=ally_action,
        ally_ban_hovers=frozenset(ally_ban_hovers - taken),
        ally_bans=ally_bans,
    )
