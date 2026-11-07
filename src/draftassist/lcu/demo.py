"""LCU-shaped champ select sessions from a simulated draft, for demo mode and parser tests.

Mirrors the documented `/lol-champ-select/v1/session` shape: actions grouped in turns, myTeam /
theirTeam with cellIds and assignedPosition, timer. Our pick turns emit a hover event before the
lock-in, like a teammate selecting a champion before locking it.
"""
from __future__ import annotations

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import DraftFormat, Side
from draftassist.engine.model import ROLES
from draftassist.engine.simulate import run_draft

LCU_POSITION = {"top": "top", "jungle": "jungle", "mid": "middle", "adc": "bottom", "support": "utility"}


def _cells(side: Side) -> list[int]:
    return list(range(5)) if side == "blue" else list(range(5, 10))


def _turns(fmt: DraftFormat) -> list[list[int]]:
    """Step indices grouped into LCU turns: simultaneous bans share a turn in standard ranked,
    and consecutive picks by one side share a turn."""
    turns: list[list[int]] = []
    for step in fmt.steps:
        prev = fmt.steps[turns[-1][-1]] if turns else None
        simultaneous_bans = fmt.name == "ranked" and step.kind == "ban" and prev is not None and prev.kind == "ban"
        same_side_picks = step.kind == "pick" and prev is not None and prev.kind == "pick" and prev.side == step.side
        if simultaneous_bans or same_side_picks:
            turns[-1].append(step.index)
        else:
            turns.append([step.index])
    return turns


def build_session(fmt: DraftFormat, our_side: Side, actions: tuple[int, ...], actor_cells: list[int],
                  positions: dict[int, str], hover: int = 0, finalization: bool = False,
                  game_id: int = 0) -> dict:
    their_side = "red" if our_side == "blue" else "blue"
    n_done = len(actions)
    lcu_actions = []
    for turn in _turns(fmt):
        group = []
        for i in turn:
            step = fmt.steps[i]
            done = i < n_done
            in_progress = (not done and not finalization and i == n_done) or (
                not done and fmt.name == "ranked" and step.kind == "ban" and n_done < 10 and fmt.steps[n_done].kind == "ban")
            champ = actions[i] if done else (hover if in_progress and step.kind == "pick" and step.side == our_side else 0)
            group.append({
                "id": i + 1, "actorCellId": actor_cells[i], "championId": champ, "completed": done,
                "isAllyAction": step.side == our_side, "isInProgress": in_progress, "pickTurn": i + 1, "type": step.kind,
            })
        lcu_actions.append(group)

    locked = {actor_cells[i]: c for i, c in enumerate(actions) if fmt.steps[i].kind == "pick"}
    hover_cell = actor_cells[n_done] if hover and n_done < len(fmt) else None

    def member(cell: int, side: Side) -> dict:
        selected = locked.get(cell) or (hover if cell == hover_cell else 0)
        return {"cellId": cell, "championId": selected, "championPickIntent": 0, "team": 1 if side == "blue" else 2,
                "assignedPosition": positions.get(cell, "") if side == our_side else "",
                "gameName": f"Player{cell}", "tagLine": "EUW", "puuid": f"puuid-{cell}", "spell1Id": 4, "spell2Id": 14}

    return {
        "actions": lcu_actions,
        "bans": {"myTeamBans": [], "theirTeamBans": [], "numBans": 10},
        "benchEnabled": False, "gameId": game_id, "isSpectating": False, "isCustomGame": True,
        "localPlayerCellId": _cells(our_side)[0],
        "myTeam": [member(c, our_side) for c in _cells(our_side)],
        "theirTeam": [member(c, their_side) for c in _cells(their_side)],
        "timer": {"adjustedTimeLeftInPhase": 0 if finalization else 27000, "isInfinite": False,
                  "phase": "FINALIZATION" if finalization else "BAN_PICK", "totalTimeInPhase": 30000},
    }


def demo_sessions(engine: DraftEngine, fmt: DraftFormat, our_side: Side, game_id: int = 0) -> list[dict]:
    """Session events for a full simulated draft (our side follows the engine's top suggestion)."""
    final, _ = run_draft(engine, fmt, our_side)
    our_roles = engine.evaluate(final).our_roles
    their_side = "red" if our_side == "blue" else "blue"
    our_cells, their_cells = _cells(our_side), _cells(their_side)

    # Our players sit in role order; whoever plays the picked champion's role makes that pick.
    positions = {cell: LCU_POSITION[r] for cell, r in zip(our_cells, ROLES)}
    cell_for_role = {r: cell for cell, r in zip(our_cells, ROLES)}
    actor_cells, bans_made, their_picks = [], {our_side: 0, their_side: 0}, 0
    for step, champ in zip(fmt.steps, final.actions):
        if step.kind == "ban":
            cells = our_cells if step.side == our_side else their_cells
            actor_cells.append(cells[bans_made[step.side] % 5])
            bans_made[step.side] += 1
        elif step.side == our_side:
            actor_cells.append(cell_for_role[our_roles[champ]])
        else:
            actor_cells.append(their_cells[their_picks])
            their_picks += 1

    events = [build_session(fmt, our_side, (), actor_cells, positions, game_id=game_id)]
    for i, champ in enumerate(final.actions):
        if fmt.steps[i].kind == "pick" and fmt.steps[i].side == our_side:
            events.append(build_session(fmt, our_side, final.actions[:i], actor_cells, positions, hover=champ, game_id=game_id))
        events.append(build_session(fmt, our_side, final.actions[: i + 1], actor_cells, positions, game_id=game_id))
    events.append(build_session(fmt, our_side, final.actions, actor_cells, positions, finalization=True, game_id=game_id))
    return events


