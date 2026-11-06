"""Players and pools (Pools page): Riot IDs and brackets, pool entries, text import, tier accept and lock."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request

from draftassist.api.deps import db_of
from draftassist.data.pools import (
    import_pools,
    list_players,
    load_pools,
    parse_pool_text,
    remove_pool_entry,
    set_players,
    set_pool_entry,
)
from draftassist.data.settings_store import get_engine_settings
from draftassist.engine.blind import pool_health
from draftassist.engine.comp import badge
from draftassist.learning.comfort import accept as accept_tiers
from draftassist.learning.comfort import set_locked, wr_adj
from draftassist.learning.execution_comfort import execution_hint, lane_z_by_entry

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


# ---------- players & pools ----------
@router.get("/api/players")
def get_players(request: Request) -> list[dict]:
    return list_players(db_of(request.app.state.services))

@router.put("/api/players")
def put_players(request: Request, players: list[dict] = Body(..., embed=True)) -> list[dict]:
    svc: Services = request.app.state.services
    resolver = None
    if svc.settings is not None and svc.settings.has_riot_api_key:
        from draftassist.data.riot import RiotClient

        riot = RiotClient(svc.settings.riot_api_key, svc.settings.cache_dir)
        resolver = riot.account_puuid
    try:
        stored = set_players(db_of(svc), players, resolver)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    svc.reload()
    return stored

@router.get("/api/pools")
def get_pools(request: Request) -> dict:
    return _pools_payload(request.app.state.services)

def _pool_changed(svc: Services) -> dict:
    """After an edit: rebuild the engine (not mid-draft: suggestions must not change under the shot-caller)."""
    applied = not svc.in_draft()
    if applied:
        svc.reload()
    return {**_pools_payload(svc), "applied": applied}

@router.put("/api/pools/{role}/{champ_id}")
def put_pool_entry(request: Request, role: str, champ_id: int, tier: str = Body(..., embed=True)) -> dict:
    """Add a champion to a player's pool, or change its tier."""
    svc: Services = request.app.state.services
    if champ_id not in svc.catalog.champs:
        raise HTTPException(404, "unknown champion")
    try:
        set_pool_entry(db_of(svc), role, champ_id, tier)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return _pool_changed(svc)

@router.delete("/api/pools/{role}/{champ_id}")
def delete_pool_entry(request: Request, role: str, champ_id: int) -> dict:
    svc: Services = request.app.state.services
    if not remove_pool_entry(db_of(svc), role, champ_id):
        raise HTTPException(404, "not in that pool")
    return _pool_changed(svc)

def _pools_payload(svc: Services) -> dict:
    db = db_of(svc)
    pools = load_pools(db)
    profiles = svc.profiles()
    tier_order = {"main": 0, "comfortable": 1, "learning": 2}
    lane_z = lane_z_by_entry(db) if get_engine_settings(db)["execution_comfort"] else None

    def hint(e) -> dict | None:  # docs/draft-vs-execution.md, Stage 6: a hint only, tiers and comfort unchanged
        if lane_z is None:
            return None
        return execution_hint(wr_adj(e.manual_tier, e.games, e.wins), lane_z.get((e.role, e.champ_id), []))

    return {
        "players": list_players(db),
        "learning": svc.learning or {"running": False},
        "pools": {r: [{"champ": svc.catalog.card(e.champ_id), "manual_tier": e.manual_tier,
                       "badge": badge(profiles, e.champ_id, r).as_dict(),
                       "scored_tier": e.scored_tier, "tier_locked": e.tier_locked, "games": e.games, "wins": e.wins,
                       "comfort": round(e.comfort, 2),
                       "last_played": e.last_played.isoformat() if e.last_played else None,
                       "proposal": (e.scored_tier if e.scored_tier and e.scored_tier != e.manual_tier
                                    and not e.tier_locked else None),
                       "execution_hint": hint(e)}
                      for e in sorted(entries, key=lambda e: (tier_order.get(e.manual_tier, 9), e.champ_id))]
                  for r, entries in pools.items()},
        "using_placeholder_pools": not any(pools.values()),
        "health": {r: pool_health(svc.engine, r) for r in pools if pools[r]},
        "champions": sorted(({"id": c, "name": name} for c, (_, name) in svc.catalog.champs.items()),
                            key=lambda c: c["name"]),
    }

@router.post("/api/pools/import")
def post_pool_import(request: Request, text: str = Body(..., embed=True)) -> dict:
    svc: Services = request.app.state.services
    db = db_of(svc)
    parsed = parse_pool_text(text, svc.catalog.champs)
    if not parsed.ok:
        raise HTTPException(422, {"errors": parsed.errors})
    counts = import_pools(db, parsed)
    svc.reload()
    return {"imported": counts}

# ---------- learning ----------
@router.post("/api/pools/accept")
def post_accept(request: Request, role: str = Body(...), champ_id: int | None = Body(None)) -> dict:
    svc: Services = request.app.state.services
    changed = accept_tiers(db_of(svc), role, champ_id)
    svc.reload()
    return {"changed": changed}

@router.post("/api/pools/lock")
def post_lock(request: Request, role: str = Body(...), champ_id: int = Body(...), locked: bool = Body(...)) -> dict:
    svc: Services = request.app.state.services
    if not set_locked(db_of(svc), role, champ_id, locked):
        raise HTTPException(404, "no such pool entry")
    svc.reload()
    return {"locked": locked}
