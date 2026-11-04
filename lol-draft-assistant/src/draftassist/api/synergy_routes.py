"""Synergy v2 routes (docs/synergy-v2.md): Settings › Synergy, the Prep page (visualiser, plan bans and picks),
the pinned plan, the synergy bracket, the Tags (Champions) and Rules pages, and the evaluate API.

Split out of api/main.py; every route reads the app's Services from `request.app.state.services`.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request
from sqlmodel import Session, select

from draftassist.api.deps import db_of
from draftassist.api.prep_view import SCOPES, VIEWS, build_prep_view
from draftassist.api.synergy import draft_context
from draftassist.data.pools import list_players
from draftassist.data.rules import set_prior
from draftassist.data.settings_store import (
    get_synergy_bracket,
    save_pinned,
    save_synergy_bracket,
    settings_payload,
)
from draftassist.data.synergy_model import fit_synergy_model, load_synergy_model
from draftassist.data.archetypes import ARCHETYPES, resolve_archetype
from draftassist.data.tags import load_archetypes, load_kit, remove_tag, update_tag
from draftassist.data.traits import load_traits
from draftassist.db.models import Champion
from draftassist.engine.comp import STYLE_LABELS, STYLES, TIMING_LABELS, badge
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.state import DraftState
from draftassist.engine.traits import KIT_TAGS, TAGS

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


@router.put("/api/settings/synergy")
def put_synergy_settings(request: Request, values: dict[str, float] = Body(..., embed=True)) -> dict:
    """Synergy v2 weights (Overall = Stat / Style / Team), confidence k, prior SD of the fits, ban d."""
    from draftassist.data.settings_store import save_synergy_v2_settings

    svc: Services = request.app.state.services
    db = db_of(svc)
    try:
        save_synergy_v2_settings(db, **values)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e)) from None
    if "prior_sd_pp" in values:
        fit_synergy_model(db, style_path=svc.style_rules_path, team_path=svc.team_rules_path)
    svc.invalidate_synergy()
    return settings_payload(db)

# ---------- synergy ----------
@router.post("/api/prep/synergy")
def post_prep_synergy(request: Request, scope: str = Body("teamfight"), view: str = Body("overall"),
                      threshold_pp: float = Body(0.5), locks: dict[str, int] | None = Body(None),
                      columns: dict[str, list[int]] | None = Body(None), use_draft: bool = Body(False)) -> dict:
    """The Prep page's top half (docs/synergy-v2.md, Stage 4). Read-only; POST because the selection is JSON."""
    svc: Services = request.app.state.services
    if scope not in SCOPES or view not in VIEWS:
        raise HTTPException(422, f"scope must be one of {', '.join(SCOPES)}; view one of {', '.join(VIEWS)}")
    for r in (*(columns or {}), *(locks or {})):
        if r not in ROLES:
            raise HTTPException(422, f"unknown role {r!r}")
    engine, live = svc.engine, svc.live.current
    unavailable: set[int] = set()
    all_locks = dict(locks or {})
    draft = {"live": False}
    enemy: list[tuple[int, str]] = []
    if use_draft and live is not None:
        ev = engine.evaluate(live.state)
        unavailable, draft_locks = draft_context(live, ev.our_roles)
        all_locks |= draft_locks
        enemy = [(c, r) for c, r in ev.their_roles.items()]
        draft = {"live": True, "locks": draft_locks, "unavailable": sorted(unavailable)}
    players = {p["role"]: p["riot_id"] for p in list_players(svc.db)} if svc.db is not None else {}
    body = build_prep_view(svc.synergy_v2(), svc.catalog, engine.team, scope, view, threshold_pp, all_locks,
                           columns, unavailable, players, enemy, engine.data.profiles)
    champs = sorted((c for c in (svc.catalog.card(i) for i in svc.catalog.champs) if c), key=lambda c: c["name"])
    return {**body, "draft": draft, "champions": champs,
            "synergy_bracket": get_synergy_bracket(svc.db) if svc.db is not None else "all"}

@router.post("/api/prep/plan")
def post_prep_plan(request: Request, locks: dict[str, int] = Body(...), side: str = Body("blue"),
                   format: str = Body("tournament"), use_draft: bool = Body(False)) -> dict:
    """The Prep page's bottom half (docs/synergy-v2.md, Stage 5): Win lane / Win teamfights / Combined
    bans, pick order by side, fills for the other roles, plan risks and backups for the plan (the locks)."""
    from draftassist.api.synergy_v2 import plan_json
    from draftassist.data.settings_store import get_synergy_v2_settings
    from draftassist.engine.formats import STANDARD_RANKED
    from draftassist.engine.plan import PlanContext

    svc: Services = request.app.state.services
    if side not in ("blue", "red") or format not in ("tournament", "flex"):
        raise HTTPException(422, "side must be blue or red; format tournament or flex")
    if any(r not in ROLES for r in locks) or len(set(locks.values())) != len(locks):
        raise HTTPException(422, f"locks: one champion per role, roles from {', '.join(ROLES)}")
    live = svc.live.current
    if use_draft and live is not None:
        state = live.state
    else:
        state = DraftState(STANDARD_RANKED if format == "flex" else TOURNAMENT, side)
    d = get_synergy_v2_settings(svc.db)["ban_teamfight_d"] if svc.db is not None else 1.0
    ctx = PlanContext(svc.engine, svc.synergy_v2(), dict(locks), state)
    return {**plan_json(ctx, svc.catalog.card, d), "live": bool(use_draft and live is not None)}

# ---------- pinned plan (docs/synergy-v2.md, Stage 6) ----------
@router.get("/api/plan/pin")
def get_pin(request: Request) -> dict:
    return {"pinned": request.app.state.services.pinned}

@router.post("/api/plan/pin")
def post_pin(request: Request, locks: dict[str, int] = Body(..., embed=True)) -> dict:
    """"Pin for next game": the plan with its pick order and Combined bans for both sides and formats.
    Live then shows the plan banner and gives the plan's bans and due picks a bonus."""
    from datetime import datetime, timezone

    from draftassist.data.settings_store import get_synergy_v2_settings
    from draftassist.engine.plan import build_pin

    svc: Services = request.app.state.services
    if not locks or any(r not in ROLES for r in locks) or len(set(locks.values())) != len(locks):
        raise HTTPException(422, f"locks: one champion per role (at least one), roles from {', '.join(ROLES)}")
    v = get_synergy_v2_settings(svc.db) if svc.db is not None else {}
    pin = build_pin(svc.engine, svc.synergy_v2(), locks, v.get("plan_ban_bonus", 0.3),
                    v.get("plan_pick_bonus", 0.3), v.get("ban_teamfight_d", 1.0))
    pin["pinned_at"] = datetime.now(timezone.utc).isoformat()
    svc.pinned = pin
    if svc.db is not None:
        save_pinned(svc.db, pin)
    svc.live.refresh()
    return {"pinned": pin}

@router.delete("/api/plan/pin")
def delete_pin(request: Request) -> dict:
    """Unpin (one click on the Live screen). The Live screen updates at once."""
    svc: Services = request.app.state.services
    svc.pinned = None
    if svc.db is not None:
        save_pinned(svc.db, None)
    svc.live.refresh()
    return {"pinned": None}

@router.put("/api/synergy/bracket")
def put_synergy_bracket(request: Request, bracket: str = Body(..., embed=True)) -> dict:
    svc: Services = request.app.state.services
    try:
        save_synergy_bracket(db_of(svc), bracket)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if svc.in_draft():
        return {"synergy_bracket": bracket, "applied": False}  # suggestions must not change mid-draft
    svc.reload()  # the engine's synergy term and the Prep page use the same table
    return {"synergy_bracket": bracket, "applied": True}

# ---------- judgement tags (docs/synergy-v2.md, Stage 1b) ----------
def _archetype_rows(champ: Champion, written: dict, tagged_roles) -> dict:
    """Roles the champion is played or tagged in, plus any with a written archetype (docs/matchup-archetypes.md)."""
    roles = {*(champ.roles or []), *tagged_roles, *(r for c, r in written if c == champ.champ_id)}
    out = {}
    for role in roles:
        a = resolve_archetype(written, champ.champ_id, role, champ.tags)
        out[role] = {"archetype": a.primary, "archetype2": a.secondary, "source": a.source,
                     "reviewed": a.reviewed, "untagged": a.untagged}
    return out

def _tag_row(svc: Services, champ: Champion, traits, prof=None, written=None, kits=None) -> dict:
    entries = {role: {"values": dict(e.values), "reviewed": e.reviewed}
               for (c, role), e in traits.tags.items() if c == champ.champ_id}
    written = written if written is not None else load_archetypes(db_of(svc))
    kits = kits if kits is not None else load_kit(db_of(svc))
    kit = {r: dict(e.values) for (c, r), e in kits.items() if c == champ.champ_id}
    measured = {role: {k: (None if v is None else round(v, 3)) for k, v in m.items()}
                for (c, role), m in traits.measured.items() if c == champ.champ_id}
    prof = prof if prof is not None else svc.profiles()
    timing = {}
    for role in champ.roles or []:
        tc = prof.curve(champ.champ_id, role)
        timing[role] = None if tc is None else {"curve": round(tc.curve, 3), "games": tc.games, "label": tc.label}
    identity = {}
    for role in {*(champ.roles or []), *entries}:
        st = prof.style(champ.champ_id, role)
        if st is not None:
            identity[role] = {"badge": badge(prof, champ.champ_id, role).as_dict(), "source": st.source,
                              "shares": {k: round(v, 3) for k, v in zip(STYLES, st.weights)}}
    return {"champ_id": champ.champ_id, "champ": svc.catalog.card(champ.champ_id), "key": champ.key,
            "timing": timing, "identity": identity, "name": champ.name, "roles": champ.roles or [],
            "attack_range": champ.attack_range, "entries": entries, "measured": measured,
            "untagged": not entries, "archetypes": _archetype_rows(champ, written, {*entries, *kit}), "kit": kit}

def _tag_one(svc: Services, champ_id: int) -> dict:
    with Session(db_of(svc)) as s:
        champ = s.get(Champion, champ_id)
    if champ is None:
        raise HTTPException(404, "unknown champion")
    return _tag_row(svc, champ, load_traits(db_of(svc)))

@router.get("/api/tags")
def get_tags(request: Request) -> dict:
    svc: Services = request.app.state.services
    traits = load_traits(db_of(svc))
    with Session(db_of(svc)) as s:
        champs = sorted(s.exec(select(Champion)).all(), key=lambda c: c.name)
    prof = svc.profiles()
    written = load_archetypes(db_of(svc))
    kits = load_kit(db_of(svc))
    rows = [_tag_row(svc, c, traits, prof, written, kits) for c in champs]
    entries = [e for r in rows for e in r["entries"].values()]
    return {"tags": list(TAGS), "roles": list(ROLES), "role_labels": ROLE_LABELS, "styles": STYLE_LABELS,
            "archetypes": {r: list(words) for r, words in ARCHETYPES.items()}, "kit_tags": list(KIT_TAGS),
            "timings": TIMING_LABELS, "version": svc.catalog.patch, "champions": rows,
            "counts": {"champions": len(rows), "entries": len(entries),
                       "reviewed": sum(e["reviewed"] for e in entries),
                       "untagged": sum(r["untagged"] for r in rows)}}

@router.put("/api/tags/{champ_id}/{role}")
def put_tag(request: Request, champ_id: int, role: str, values: dict[str, int] = Body(...),
            reviewed: bool = Body(...), archetype: str | None = Body(None),
            archetype2: str | None = Body(None), kit: dict[str, int] | None = Body(None)) -> dict:
    svc: Services = request.app.state.services
    _tag_one(svc, champ_id)
    try:
        update_tag(db_of(svc), svc.tags_path, champ_id, role, values, reviewed, archetype, archetype2, kit)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    svc.invalidate_synergy()
    svc.refresh_profiles()  # fight-style badges and comp labels come from the tags
    return _tag_one(svc, champ_id)

@router.delete("/api/tags/{champ_id}/{role}")
def delete_tag(request: Request, champ_id: int, role: str) -> dict:
    svc: Services = request.app.state.services
    remove_tag(db_of(svc), svc.tags_path, champ_id, role)
    svc.invalidate_synergy()
    svc.refresh_profiles()  # fight-style badges and comp labels come from the tags
    return _tag_one(svc, champ_id)

# ---------- synergy v2 rules (docs/synergy-v2.md, Stage 2) ----------
def _rules_payload(svc: Services) -> dict:
    from draftassist.data.settings_store import get_synergy_v2_settings
    from draftassist.engine.style import CODE_OF, flagged

    db = db_of(svc)
    model = load_synergy_model(db, style_path=svc.style_rules_path, team_path=svc.team_rules_path)

    def row(kind: str, rule, extra: dict) -> dict:
        f = model.fits.get((kind, rule.id))
        fitted = None if f is None or f.prior_pp != rule.prior_pp else round(25 * f.value, 2)
        return {"id": rule.id, "note": rule.note, "prior_pp": rule.prior_pp, "fitted_pp": fitted,
                "support": f.support if f else 0,
                "flagged": fitted is not None and flagged(rule.prior_pp, fitted), **extra}

    style = [row("style", r, {"pair": r.pair, "when": {CODE_OF[role]: t for role, t in r.when}})
             for r in model.style.rules]
    team = [row("team", r, {"cross": r.cross, "feature": r.feature, "ours": r.ours, "theirs": r.theirs})
            for r in model.team.rules]
    return {"window": model.window, "matches": model.matches,
            "prior_sd_pp": get_synergy_v2_settings(db)["prior_sd_pp"], "style": style, "team": team}

@router.get("/api/rules")
def get_rules(request: Request) -> dict:
    return _rules_payload(request.app.state.services)

@router.put("/api/rules")
def put_rule(request: Request, kind: str = Body(...), id: str = Body(...), prior_pp: float = Body(...)) -> dict:
    """Edit one rule's prior: written back to its YAML file, then every rule is refitted."""
    svc: Services = request.app.state.services
    try:
        set_prior(kind, id, prior_pp, svc.style_rules_path, svc.team_rules_path)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    fit_synergy_model(db_of(svc), style_path=svc.style_rules_path, team_path=svc.team_rules_path)
    svc.invalidate_synergy()
    rows = _rules_payload(svc)["style" if kind == "style" else "team"]
    return next(r for r in rows if r["id"] == id)

@router.post("/api/rules/refit")
def post_refit(request: Request) -> dict:
    svc: Services = request.app.state.services
    try:
        result = fit_synergy_model(db_of(svc), style_path=svc.style_rules_path, team_path=svc.team_rules_path)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    svc.invalidate_synergy()
    return result

# ---------- synergy v2: evaluate (docs/synergy-v2.md, Stage 3) ----------
def _comp_input(svc: Services, champions: list[dict], enemies: list[dict] | None):
    comp: dict[str, int] = {}
    for item in champions:
        role, champ = item.get("role"), item.get("champ_id")
        if role not in ROLES or not isinstance(champ, int) or role in comp:
            raise HTTPException(422, "champions: one {champ_id, role} per role, roles from " + ", ".join(ROLES))
        comp[role] = champ
    if len(set(comp.values())) != len(comp):
        raise HTTPException(422, "a champion can only fill one role")
    enemies = enemies or []
    if len(enemies) > 5:
        raise HTTPException(422, "at most 5 enemy champions")
    known = {e["champ_id"]: e["role"] for e in enemies if e.get("role") in ROLES}
    rest = [e["champ_id"] for e in enemies if e.get("role") not in ROLES]
    if rest:
        free = tuple(r for r in ROLES if r not in known.values())
        guess = svc.engine.likely_roles(rest, roles=free)
        known |= guess
    return comp, [(c, r) for c, r in known.items()]

@router.post("/api/synergy/evaluate")
def post_synergy_evaluate(request: Request, champions: list[dict] = Body(...), enemies: list[dict] | None = Body(None),
                          banned: list[int] | None = Body(None), k: int = Body(5)) -> dict:
    """Stat / Style / Team for up to 5 of our champions (any roles) and optional enemies; for missing
    roles, the top k pool champions by Overall gain. Banned and picked champions are never suggested."""
    from draftassist.api.synergy_v2 import evaluation_json
    from draftassist.engine.engine import Suggestion as EngineSuggestion
    from draftassist.engine.formats import TOURNAMENT as _T
    from draftassist.engine.guards import validate_suggestions

    svc: Services = request.app.state.services
    comp, enemy = _comp_input(svc, champions, enemies)
    ev = svc.synergy_v2()
    team = svc.engine.team
    unavailable = set(banned or []) | {c for c, _ in enemy}
    result = ev.evaluate(comp, enemy)
    suggestions = ev.suggest(comp, {r: list(team.pools.get(r, {})) for r in ROLES}, unavailable, enemy, k=k)
    # last check, as for every suggestion list: the single availability source and Rule 1
    state = DraftState(_T, "blue", provisional=frozenset(unavailable | set(comp.values())))
    for role, rows in suggestions.items():
        checked = validate_suggestions(state, "pick", [
            EngineSuggestion(s.champ, svc.catalog.card(s.champ)["name"], "pick", s.gain, "", role) for s in rows])
        keep = {s.champ_id for s in checked}
        suggestions[role] = [s for s in rows if s.champ in keep]
    tier = lambda c, r: (e.tier if (e := team.entry(c, r)) else None)
    return {"window": ev.stat.window, **evaluation_json(result, svc.catalog.card, suggestions, tier)}
