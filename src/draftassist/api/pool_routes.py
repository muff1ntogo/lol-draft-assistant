"""Players and pools (Pools page): Riot IDs and brackets, the bench and swaps (docs/player-roster.md), pool entries,
text import, tier accept and lock. Every change is mirrored to the roster file (Services.save_roster)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request

from draftassist.api.deps import db_of
from draftassist.data.pools import (
    bench,
	bench_player,
 create_player,
	delete_player,
    forget_player,
    import_pools,
    list_players,
    load_pools,
    parse_pool_text,
    remove_pool_entry,
    set_players,
    set_pool_entry,
    swap_player,
)
from draftassist.api.onboarding import OnboardingRunner
from draftassist.data.settings_store import get_engine_settings
from draftassist.engine.blind import pool_health
from draftassist.engine.comp import badge
from draftassist.learning.comfort import accept as accept_tiers
from draftassist.learning.comfort import set_locked, wr_adj
from draftassist.learning.execution_comfort import execution_hint, lane_z_by_entry
from draftassist.learning.profile import WINDOWS, load_inputs, profile, summary
from sqlmodel import Session, select
from draftassist.db.models import Player

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


# ---------- players & pools ----------
@router.get("/api/players")
def get_players(request: Request) -> list[dict]:
    return list_players(db_of(request.app.state.services))

def _resolver(svc: Services):
    """account-v1 lookups for new Riot IDs, with a key; None without (champ select links them instead)."""
    if svc.settings is None or not svc.settings.has_riot_api_key:
        return None
    from draftassist.data.riot import RiotClient

    return RiotClient(svc.settings.riot_api_key, svc.settings.cache_dir).account_puuid


def _roster_changed(svc: Services) -> None:
    svc.save_roster()
    svc.reload()


def _onboarding(svc: Services) -> OnboardingRunner:
    runner = getattr(svc, "onboarding", None)
    if runner is None:
        yield_file = svc.settings.cache_dir / "jobs" / "yield.json" if svc.settings else None
        runner = OnboardingRunner(svc.make_riot(), lambda: svc.live.engine._enemy_role_shares(), yield_file)
        svc.onboarding = runner
    return runner


@router.post("/api/players/lookup")
def post_lookup(request: Request, riot_id: str = Body(..., embed=True)) -> dict:
    svc: Services = request.app.state.services
    if not _onboarding(svc).available:
        return {"no_key": True}
    try:
        riot = svc.make_riot()()
        account = riot.account(riot_id)
        if not account:
            return {"found": False}
        summoner = riot.summoner(account["puuid"]) or {}
        return {"found": True, "riot_id": f"{account.get('gameName', riot_id.split('#')[0])}#{account.get('tagLine', riot_id.split('#')[-1])}",
                "level": summoner.get("summonerLevel"), "rank": riot.ranks(account["puuid"])}
    finally:
        if 'riot' in locals(): riot.close()


@router.post("/api/players/onboard")
def post_onboard(request: Request, riot_id: str = Body(...), role: str = Body(...)) -> dict:
    svc: Services = request.app.state.services
    if role not in {"top", "jungle", "mid", "adc", "support"}:
        raise HTTPException(422, "unknown role")
    runner = _onboarding(svc)
    return {"job": runner.start(riot_id, role)}


@router.get("/api/players/onboard/{job}")
def get_onboard(request: Request, job: str) -> dict:
    record = _onboarding(request.app.state.services).get(job)
    if record is None:
        raise HTTPException(404, "no such onboarding job")
    return {"status": record.status, "games_read": record.games_read, "games_total": record.games_total,
            "proposal": record.proposal, "also_plays": record.also_plays, "error": record.error}


@router.post("/api/players")
def post_player(request: Request, riot_id: str = Body(...), role: str = Body(...), active: bool = Body(...),
                pool: list[dict] = Body(...)) -> list[dict]:
    svc: Services = request.app.state.services
    try:
        stored = create_player(db_of(svc), riot_id, role, active, pool, _onboarding(svc).completed(riot_id, role))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    _roster_changed(svc)
    return stored


@router.put("/api/players")
def put_players(request: Request, players: list[dict] = Body(..., embed=True)) -> list[dict]:
    svc: Services = request.app.state.services
    try:
        stored = set_players(db_of(svc), players, _resolver(svc))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    _roster_changed(svc)
    return stored


@router.post("/api/players/swap")
def post_swap(request: Request, role: str = Body(...), riot_id: str = Body(...)) -> dict:
    """Put a sub in (or someone back from the bench) for a role; the current player is benched with their pool. Like a
    pool edit, it reaches suggestions after the current draft."""
    svc: Services = request.app.state.services
    try:
        swap_player(db_of(svc), role, riot_id, _resolver(svc))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    svc.save_roster()
    if svc.players is not None:
        svc.players.invalidate()
    out = _pool_changed(svc)
    svc.learn()  # their recent games and comfort, now that they're playing
    return out


@router.delete("/api/players/bench")
def delete_benched(request: Request, riot_id: str) -> dict:
    """Forget a benched player for good: their pool and stored games too."""
    svc: Services = request.app.state.services
    if not forget_player(db_of(svc), riot_id):
        raise HTTPException(404, "no benched player with that Riot ID")
    svc.save_roster()
    return _pools_payload(svc)


def _player(svc: Services, riot_id: str) -> Player | None:
 with Session(db_of(svc)) as session:
  return next((p for p in session.exec(select(Player)).all() if p.riot_id.lower() == riot_id.lower()), None)


def _profile_payload(svc: Services, player: Player, window: str = "60d") -> dict:
 inputs = load_inputs(db_of(svc), player.puuid)
 body = profile(inputs.games, inputs.pool, inputs.execution, player.role, inputs.mastery, inputs.rank, window)
 lane_z = lane_z_by_entry(db_of(svc)) if get_engine_settings(db_of(svc))["execution_comfort"] else {}
 from draftassist.engine.blind import blind_safety
 for group in body["pools"]:
  for row in group["pool"]:
   row["champ"] = svc.catalog.card(row["champ_id"])
   row["execution_hint"] = execution_hint(wr_adj(row["tier"], row["games"], row["wins"]), lane_z.get((group["role"], row["champ_id"]), [])) if lane_z else None
   row["blind_safety"] = round(blind_safety(svc.engine, row["champ_id"], group["role"]), 2)
  for row in group["not_in_pool"]:
   row["champ"] = svc.catalog.card(row["champ_id"])
 body.update(riot_id=player.riot_id, active=player.active, bracket=player.bracket,
             active_since=player.active_since.isoformat() if player.active_since else None,
             health=pool_health(svc.engine, player.role))
 return body


@router.get("/api/roster")
def get_roster(request: Request) -> dict:
 svc: Services = request.app.state.services
 db = db_of(svc)
 with Session(db) as session:
  players = session.exec(select(Player)).all()

 def item(p: Player) -> dict:
  inputs = load_inputs(db, p.puuid)
  row = summary(inputs.games, inputs.pool, inputs.execution, p.role, inputs.rank)
  row.update(riot_id=p.riot_id, active=p.active, bracket=p.bracket,
   active_since=p.active_since.isoformat() if p.active_since else None,
   health=pool_health(svc.engine, p.role) if p.active and p.riot_id else None)
  row["pool"]["champs"] = [{**entry, "champ": svc.catalog.card(entry["champ_id"])}
   for entry in row["pool"]["champs"]]
  return row

 active = {p.role: item(p) for p in players if p.active and p.riot_id}
 return {"lineup": [active.get(role, {"role": role, "riot_id": "", "active": False})
  for role in ("top", "jungle", "mid", "adc", "support")],
  "bench": [item(p) for p in players if not p.active and p.riot_id]}


@router.get("/api/players/{riot_id}/profile")
def get_player_profile(request: Request, riot_id: str, window: str = "60d") -> dict:
 if window not in WINDOWS:
  raise HTTPException(422, "window must be 20 or 60d")
 svc: Services = request.app.state.services
 player = _player(svc, riot_id)
 if player is None:
  raise HTTPException(404, "no player with that Riot ID")
 return _profile_payload(svc, player, window)


@router.post("/api/players/{riot_id}/bench")
def post_bench_player(request: Request, riot_id: str) -> dict:
 svc: Services = request.app.state.services
 if not bench_player(db_of(svc), riot_id):
  raise HTTPException(404, "no active player with that Riot ID")
 _roster_changed(svc)
 return get_roster(request)


@router.post("/api/players/{riot_id}/activate")
def post_activate_player(request: Request, riot_id: str, role: str = Body(..., embed=True)) -> dict:
 svc: Services = request.app.state.services
 previous = next((p["riot_id"] for p in list_players(db_of(svc)) if p["role"] == role), "")
 try:
  swap_player(db_of(svc), role, riot_id, _resolver(svc))
 except ValueError as e:
  raise HTTPException(422, str(e)) from None
 _roster_changed(svc)
 svc.learn()
 return {"roster": get_roster(request), "undo": {"riot_id": previous, "role": role} if previous else None}


@router.delete("/api/players/{riot_id}")
def delete_player_route(request: Request, riot_id: str, confirm: str) -> dict:
 if confirm.lower() != riot_id.lower():
  raise HTTPException(422, "confirm must repeat the Riot ID")
 svc: Services = request.app.state.services
 if not delete_player(db_of(svc), riot_id):
  raise HTTPException(404, "no player with that Riot ID")
 _roster_changed(svc)
 return get_roster(request)


@router.get("/api/pools")
def get_pools(request: Request) -> dict:
    return _pools_payload(request.app.state.services)

def _pool_changed(svc: Services) -> dict:
    """After an edit: rebuild the engine (not mid-draft: suggestions must not change under the shot-caller)."""
    svc.save_roster()
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
        "bench": [{**p, "pools": {r: [{"champ": svc.catalog.card(c), "tier": t} for c, t in champs]
                                  for r, champs in p["pools"].items()}} for p in bench(db)],
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
    _roster_changed(svc)
    return {"imported": counts}

# ---------- learning ----------
@router.post("/api/pools/accept")
def post_accept(request: Request, role: str = Body(...), champ_id: int | None = Body(None)) -> dict:
    svc: Services = request.app.state.services
    changed = accept_tiers(db_of(svc), role, champ_id)
    _roster_changed(svc)
    return {"changed": changed}

@router.post("/api/pools/lock")
def post_lock(request: Request, role: str = Body(...), champ_id: int = Body(...), locked: bool = Body(...)) -> dict:
    svc: Services = request.app.state.services
    if not set_locked(db_of(svc), role, champ_id, locked):
        raise HTTPException(404, "no such pool entry")
    _roster_changed(svc)
    return {"locked": locked}
