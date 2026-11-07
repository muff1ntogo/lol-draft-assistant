"""Enemy scouting (docs/enemy-scouting.md): who we face in the current game, and what's known about them."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Query, Request

from draftassist.api.deps import db_of
from draftassist.data.scout import assign_roles, load_pools, parse_riot_ids, scout_summary

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


def _payload(svc: Services, riot_ids: list[str], source: str | None, given: dict | None = None) -> dict:
    """The same summary Live shows (data/scout.scout_summary), for any list of Riot IDs."""
    runner = svc.scouting()
    pools = load_pools(db_of(svc), riot_ids)
    players = scout_summary(riot_ids, pools, assign_roles(pools, given or dict.fromkeys(riot_ids)), svc.catalog.card)
    return {"source": source, "available": runner.available, "running": runner.running, "error": runner.last_error,
            "players": players}


def _current(svc: Services) -> dict:
    """The opponents Live scouts now: the client's, else the pasted ones."""
    ids, given, source = svc.opponents()
    return _payload(svc, ids, source, given)


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
def get_scout(request: Request, ids: list[str] = Query(default=[])) -> dict:  # noqa: B008 - FastAPI's own idiom
    """With `ids`: those players. Without: the current game's opponents."""
    svc: Services = request.app.state.services
    return _payload(svc, ids, None) if ids else _current(svc)
