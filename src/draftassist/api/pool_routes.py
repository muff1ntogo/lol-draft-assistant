"""Players and pools (Pools page): Riot IDs and brackets, the bench and swaps (docs/player-roster.md), pool entries,
text import, tier accept and lock. Every change is mirrored to the roster file (Services.save_roster)."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request
from sqlmodel import Session, select

from draftassist.api.deps import db_of
from draftassist.api.onboarding import OnboardingRunner, lookup
from draftassist.data.pools import (
    ROLE_ALIASES,
    bench,
    bench_player,
    create_player,
    delete_player,
    import_pools,
    list_players,
    load_pools,
    parse_pool_text,
    remove_player_pool_entry,
    remove_pool_entry,
    set_player_pool_entry,
    set_players,
    set_pool_entry,
    swap_player,
)
from draftassist.data.riot import RiotAuthError
from draftassist.data.settings_store import get_engine_settings
from draftassist.db.models import Player, PoolEntry
from draftassist.engine.blind import blind_safety, pool_health
from draftassist.engine.comp import badge
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.learning.comfort import accept as accept_tiers
from draftassist.learning.comfort import set_locked, wr_adj
from draftassist.learning.execution_comfort import execution_hint, lane_z_by_entry
from draftassist.learning.profile import WINDOWS, load_inputs, profile, summary

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


def _lineup_changed(svc: Services) -> None:
    """After a player is added, swapped, benched or deleted: mirror the roster file and forget the cached player
    directory at once, but rebuild the engine only between drafts, so suggestions never change under the shot-caller.
    Mid-draft the change is picked up when the draft ends (the stats version has moved)."""
    svc.save_roster()
    if svc.players is not None:
        svc.players.invalidate()
    if not svc.in_draft():
        svc.reload()


# ---------- adding a player (docs/roster-profiles.md: lookup, read their games, review, create) ----------
def _onboarding(svc: Services) -> OnboardingRunner:
    runner = getattr(svc, "onboarding", None)
    if runner is None:
        yield_file = svc.settings.cache_dir / "jobs" / "yield.json" if svc.settings else None
        runner = OnboardingRunner(svc.make_riot, lambda: svc.live.engine.role_share_map, yield_file)
        svc.onboarding = runner
    return runner


@router.post("/api/players/lookup")
def post_lookup(request: Request, riot_id: str = Body(..., embed=True)) -> dict:
    """Who a Riot ID is: `{found, riot_id (as the account spells it), level, rank}`, `{found: false}`, `{no_key: true}`
    without a Riot key, or `{on_roster: true, ...}` when we already have them."""
    svc: Services = request.app.state.services
    known = _player(svc, riot_id.strip())
    if known is not None:
        return {"found": True, "on_roster": True, "riot_id": known.riot_id, "role": known.role, "active": known.active}
    factory = svc.make_riot()
    if factory is None:
        return {"no_key": True}
    riot = factory()
    try:
        return lookup(riot, riot_id.strip())
    except RiotAuthError as e:
        raise HTTPException(502, str(e)) from None
    except Exception as e:
        raise HTTPException(502, f"Riot didn't answer ({type(e).__name__})") from None
    finally:
        riot.close()


@router.post("/api/players/onboard")
def post_onboard(request: Request, riot_id: str = Body(...), role: str = Body(...)) -> dict:
    if role not in ROLES:
        raise HTTPException(422, "unknown role")
    return {"job": _onboarding(request.app.state.services).start(riot_id.strip(), role)}


@router.get("/api/players/onboard/{job}")
def get_onboard(request: Request, job: str) -> dict:
    svc: Services = request.app.state.services
    record = _onboarding(svc).get(job)
    if record is None:
        raise HTTPException(404, "no such onboarding job")
    return _with_cards(svc, record.as_dict())


@router.post("/api/players/onboard/{job}/skip")
def post_onboard_skip(request: Request, job: str) -> dict:
    """Stop reading and go on with the games read so far."""
    svc: Services = request.app.state.services
    record = _onboarding(svc).skip(job)
    if record is None:
        raise HTTPException(404, "no such onboarding job")
    return _with_cards(svc, record.as_dict())


def _with_cards(svc: Services, body: dict) -> dict:
    body["proposal"] = [{**row, "champ": svc.catalog.card(row["champ_id"])} for row in body["proposal"]]
    return body


@router.post("/api/players")
def post_player(request: Request, riot_id: str = Body(...), role: str = Body(...), active: bool = Body(...),
                pool: list[dict] = Body(...)) -> dict:
    """Create the player and their reviewed pool in one transaction, with what onboarding read for them."""
    svc: Services = request.app.state.services
    try:
        create_player(db_of(svc), riot_id, role, active, pool, _onboarding(svc).fetched(riot_id.strip(), role))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    _lineup_changed(svc)
    svc.learn()
    return _roster_payload(svc)


# ---------- the roster page and player profiles (docs/roster-profiles.md) ----------
def _player(svc: Services, riot_id: str) -> Player | None:
    if not riot_id:
        return None
    with Session(db_of(svc)) as session:
        return next((p for p in session.exec(select(Player)).all() if p.riot_id.lower() == riot_id.lower()), None)


def _days_since(when: datetime | None) -> int | None:
    if when is None:
        return None
    when = when if when.tzinfo else when.replace(tzinfo=UTC)  # SQLite hands datetimes back without their zone
    return max(0, (datetime.now(UTC) - when).days)


def _health(svc: Services, player: Player) -> dict | None:
    """Pool health is the engine's view of the active pools, so only an active player has it."""
    if not player.active or not svc.engine.team.pools.get(player.role):
        return None
    return pool_health(svc.engine, player.role)


def _roster_row(svc: Services, player: Player, pool_roles: dict[str, list[str]]) -> dict:
    inputs = load_inputs(db_of(svc), player.puuid)
    row = summary(inputs.games, inputs.pool, inputs.execution, player.role, inputs.rank)
    row["pool"]["champs"] = [{**e, "champ": svc.catalog.card(e["champ_id"])} for e in row["pool"]["champs"]]
    row.update(riot_id=player.riot_id, active=player.active, bracket=player.bracket, resolved=player.resolved,
               role_label=ROLE_LABELS[player.role], since_days=_days_since(player.active_since) if player.active else None,
               pool_roles=pool_roles.get(player.puuid, []), health=_health(svc, player))
    return row


def _roster_payload(svc: Services) -> dict:
    db = db_of(svc)
    with Session(db) as session:
        players = [p for p in session.exec(select(Player)).all() if p.riot_id]
        has: dict[str, set[str]] = {}
        for e in session.exec(select(PoolEntry)).all():
            has.setdefault(e.puuid, set()).add(e.role)
    pool_roles = {puuid: [r for r in ROLES if r in roles] for puuid, roles in has.items()}
    active = {p.role: _roster_row(svc, p, pool_roles) for p in players if p.active}
    benched = sorted((p for p in players if not p.active), key=lambda p: (ROLES.index(p.role), p.riot_id.lower()))
    return {
        "lineup": [active.get(role) or {"role": role, "role_label": ROLE_LABELS[role], "riot_id": "", "active": False}
                   for role in ROLES],
        "bench": [_roster_row(svc, p, pool_roles) for p in benched],
        "learning": svc.learning or {"running": False},
        "has_key": svc.make_riot() is not None,
        "in_draft": svc.in_draft(),
        "champions": _champion_cards(svc),
    }


def _champion_cards(svc: Services) -> list[dict]:
    """Every champion as a card (id, name, icon), by name: what the add-champion search offers."""
    return sorted((svc.catalog.card(c) for c in svc.catalog.champs), key=lambda c: c["name"])


def _profile_payload(svc: Services, player: Player, window: str) -> dict:
    db = db_of(svc)
    inputs = load_inputs(db, player.puuid)
    body = profile(inputs.games, inputs.pool, inputs.execution, player.role, inputs.mastery, inputs.rank, window)
    lane_z = lane_z_by_entry(db) if get_engine_settings(db)["execution_comfort"] else None
    profiles = svc.profiles()
    for group in body["pools"]:
        group["role_label"] = ROLE_LABELS[group["role"]]
        group["editable_tiers"] = player.active and group["role"] == player.role  # accept and lock act on the active pool
        for row in group["pool"]:
            row["champ"] = svc.catalog.card(row["champ_id"])
            row["badge"] = badge(profiles, row["champ_id"], group["role"]).as_dict()
            row["blind_safety"] = round(blind_safety(svc.engine, row["champ_id"], group["role"]), 2)
            row["execution_hint"] = (execution_hint(wr_adj(row["tier"], row["games"], row["wins"]),
                                                    lane_z.get((group["role"], row["champ_id"]), []))
                                     if lane_z is not None and group["editable_tiers"] else None)
        for row in group["not_in_pool"]:
            row["champ"] = svc.catalog.card(row["champ_id"])
    for row in body["roles"]:
        row["role_label"] = ROLE_LABELS[row["role"]]
    for row in body["mastery"]:
        row["champ"] = svc.catalog.card(row["champ_id"])
    body.update(riot_id=player.riot_id, role_label=ROLE_LABELS[player.role], active=player.active, bracket=player.bracket,
                resolved=player.resolved, since_days=_days_since(player.active_since) if player.active else None,
                health=_health(svc, player), has_key=svc.make_riot() is not None,
                champions=_champion_cards(svc))
    return body


def _found(svc: Services, riot_id: str) -> Player:
    player = _player(svc, riot_id)
    if player is None:
        raise HTTPException(404, "no player with that Riot ID")
    return player


@router.get("/api/roster")
def get_roster(request: Request) -> dict:
    """The lineup (five roles, each its active player's summary or empty) and the bench."""
    return _roster_payload(request.app.state.services)


@router.get("/api/players/{riot_id}/profile")
def get_player_profile(request: Request, riot_id: str, window: str = "60d") -> dict:
    if window not in WINDOWS:
        raise HTTPException(422, "window must be 20 or 60d")
    svc: Services = request.app.state.services
    return _profile_payload(svc, _found(svc, riot_id), window)


@router.put("/api/players/{riot_id}/bracket")
def put_player_bracket(request: Request, riot_id: str, bracket: str = Body(..., embed=True)) -> dict:
    """The stats bracket Live reads this player's win rates in."""
    svc: Services = request.app.state.services
    player = _found(svc, riot_id)
    if not player.active:
        raise HTTPException(422, "the stats bracket belongs to the active player in a role")
    set_players(db_of(svc), [{"role": player.role, "riot_id": player.riot_id, "bracket": bracket}])
    _roster_changed(svc)
    return {"bracket": bracket.lower()}


@router.put("/api/players/{riot_id}/pool/{role}/{champ_id}")
def put_player_pool_entry(request: Request, riot_id: str, role: str, champ_id: int, tier: str = Body(..., embed=True)) -> dict:
    """Add a champion to this player's pool (active or benched), or change its tier."""
    svc: Services = request.app.state.services
    if champ_id not in svc.catalog.champs:
        raise HTTPException(404, "unknown champion")
    try:
        if not set_player_pool_entry(db_of(svc), riot_id, role, champ_id, tier):
            raise HTTPException(404, "no player with that Riot ID")
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"applied": _pool_edit_applied(svc)}


@router.delete("/api/players/{riot_id}/pool/{role}/{champ_id}")
def delete_player_pool_entry(request: Request, riot_id: str, role: str, champ_id: int) -> dict:
    svc: Services = request.app.state.services
    if not remove_player_pool_entry(db_of(svc), riot_id, role, champ_id):
        raise HTTPException(404, "not in that pool")
    return {"applied": _pool_edit_applied(svc)}


def _pool_edit_applied(svc: Services) -> bool:
    """After a pool edit: rebuild the engine, but not mid-draft (suggestions must not change under the shot-caller)."""
    svc.save_roster()
    applied = not svc.in_draft()
    if applied:
        svc.reload()
    return applied


@router.post("/api/players/{riot_id}/bench")
def post_bench_player(request: Request, riot_id: str) -> dict:
    svc: Services = request.app.state.services
    if not bench_player(db_of(svc), riot_id):
        raise HTTPException(404, "no active player with that Riot ID")
    _lineup_changed(svc)
    return _roster_payload(svc)


@router.post("/api/players/{riot_id}/activate")
def post_activate_player(request: Request, riot_id: str, role: str = Body(..., embed=True)) -> dict:
    """Put a player in as the role's active player; whoever was there goes to the bench. `undo` says how to swap
    back: activate that player in that role, or bench this one if the role was empty."""
    svc: Services = request.app.state.services
    _found(svc, riot_id)
    if role not in ROLES:
        raise HTTPException(422, "unknown role")
    previous = next((p["riot_id"] for p in list_players(db_of(svc)) if p["role"] == role), "")
    if previous.lower() == riot_id.lower():
        return {"roster": _roster_payload(svc), "undo": None}
    try:
        swap_player(db_of(svc), role, riot_id, _resolver(svc))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    _lineup_changed(svc)
    svc.learn()
    return {"roster": _roster_payload(svc), "undo": {"riot_id": previous, "role": role, "bench": riot_id}}


@router.delete("/api/players/{riot_id}")
def delete_player_route(request: Request, riot_id: str, confirm: str = "") -> dict:
    """Delete a player, their pools and their fetched games (the team's logged games stay). `confirm` must repeat the
    Riot ID: this can't be undone."""
    if confirm.lower() != riot_id.lower():
        raise HTTPException(422, "confirm must repeat the Riot ID")
    svc: Services = request.app.state.services
    if not delete_player(db_of(svc), riot_id):
        raise HTTPException(404, "no player with that Riot ID")
    _lineup_changed(svc)
    return _roster_payload(svc)


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

@router.post("/api/pools/parse")
def post_pool_parse(request: Request, text: str = Body(...), role: str = Body(...)) -> dict:
    """Read a pasted pool for one player without saving it (Add player, and "Import from text" on a profile): the
    import format, with or without the role's heading. Returns that role's `entries` and every `errors` line."""
    svc: Services = request.app.state.services
    if role not in ROLES:
        raise HTTPException(422, "unknown role")
    first = next((line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")), "")
    headed = first.rstrip(":").strip().lower() in ROLE_ALIASES
    parsed = parse_pool_text(text if headed else f"{role.upper()}\n{text}", svc.catalog.champs)
    others = [r for r in parsed.entries if r != role]
    shift = 0 if headed else 1  # the heading we added moved every line down one
    errors = [re.sub(r"^line (\d+)", lambda m: f"line {int(m.group(1)) - shift}", e) for e in parsed.errors] + ([f"only the {ROLE_LABELS[role]} pool is read here: {', '.join(others)} ignored"] if others else [])
    return {"entries": [{"champ_id": c, "champ": svc.catalog.card(c), "tier": t} for c, t in parsed.entries.get(role, [])],
            "errors": errors}


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
