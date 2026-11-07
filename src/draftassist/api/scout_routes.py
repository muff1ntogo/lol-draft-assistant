"""Enemy scouting (docs/enemy-scouting.md): who we face in the current game, and what's known about them."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Query, Request

from draftassist.api.deps import db_of
from draftassist.data.scout import load_pools, parse_riot_ids

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


def _by_ids(svc: Services, riot_ids: list[str]) -> dict:
    runner = svc.scouting()
    pools = load_pools(db_of(svc), riot_ids)
    card = svc.catalog.card
    players = []
    for rid in riot_ids:
        pool = pools.get(rid)
        entry = pool.as_dict() if pool else {"riot_id": rid, "status": "new", "games_read": 0, "games_wanted": 0,
                                            "main_role": None, "games": {}, "mastery": []}
        entry["top"] = [{"champ": card(c), "points": p} for c, p in (pool.mastery[:3] if pool else ())]
        players.append(entry)
    return {"available": runner.available, "running": runner.running, "error": runner.last_error, "players": players}


def _current(svc: Services) -> dict:
    """The opponents Live scouts now: the client's, else the pasted ones."""
    ids, _, source = svc.opponents()
    _, info = svc.scout_info()
    runner = svc.scouting()
    return info or {"source": source, "available": runner.available, "running": runner.running,
                    "error": runner.last_error, "players": [], "riot_ids": ids}


@router.post("/api/scout")
def post_scout(request: Request, text: str = Body("", embed=True), force: bool = Body(False, embed=True)) -> dict:
    """Paste the opponents' Riot IDs (one per line, or an OP.GG multi-search) for the current game. Fetching starts at
    once; Live uses them unless the client gives the enemy's names itself. Cleared when the game ends."""
    svc: Services = request.app.state.services
    riot_ids = parse_riot_ids(text)
    if not riot_ids:
        raise HTTPException(422, "no Riot IDs found: use Name#TAG, one per line")
    svc.scout_manual[:] = riot_ids
    svc.scout_asked.update(rid.lower() for rid in riot_ids)
    svc.scouting().enqueue(riot_ids, force=force)
    svc.live.refresh()
    return _current(svc)


@router.delete("/api/scout")
def delete_scout(request: Request) -> dict:
    svc: Services = request.app.state.services
    svc.scout_manual.clear()
    svc.live.refresh()
    return _current(svc)


@router.get("/api/scout")
def get_scout(request: Request, ids: list[str] = Query(default=[])) -> dict:
    """With `ids`: those players. Without: the current game's opponents."""
    svc: Services = request.app.state.services
    return _by_ids(svc, ids) if ids else _current(svc)
