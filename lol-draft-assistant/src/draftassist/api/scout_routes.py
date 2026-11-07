"""Enemy scouting (docs/enemy-scouting.md): start a fetch, read what's known."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Query, Request

from draftassist.api.deps import db_of
from draftassist.data.scout import load_pools, parse_riot_ids

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


def _payload(svc: Services, riot_ids: list[str]) -> dict:
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


@router.post("/api/scout")
def post_scout(request: Request, text: str = Body("", embed=True), force: bool = Body(False, embed=True)) -> dict:
    """Riot IDs, one per line or an OP.GG multi-search paste. Starts a background fetch."""
    svc: Services = request.app.state.services
    riot_ids = parse_riot_ids(text)
    if not riot_ids:
        raise HTTPException(422, "no Riot IDs found: use Name#TAG, one per line")
    svc.scouting().enqueue(riot_ids, force=force)
    return _payload(svc, riot_ids)


@router.get("/api/scout")
def get_scout(request: Request, ids: list[str] = Query(default=[])) -> dict:
    return _payload(request.app.state.services, ids)
