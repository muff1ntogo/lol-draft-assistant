"""Learning and prep: lane ban tables, the learning job, Team page style, the patch prep sheet, model check."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request
from sqlalchemy import text

from draftassist.api.deps import db_of
from draftassist.data.archetypes import ARCHETYPES, load_archetype_plans
from draftassist.data.matchup_model import load_matchup_model, pooled_stats
from draftassist.engine.blind import pool_health
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.lane_ban import RoleBanTable
from draftassist.engine.matchup_tree import matchup_tree
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.state import DraftState
from draftassist.learning.prep import prep_sheet
from draftassist.learning.report import model_check
from draftassist.learning.style import set_hand_style, team_summary

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


def _band(ban: float, neutral: float) -> str:
    if ban > neutral:
        return "positive"
    if ban < -neutral:
        return "negative"
    return "neutral"


def lane_ban_report(engine: DraftEngine) -> list[dict]:
    """The Prep page's per-role tables: mu, then Opp / PR / Resp / via / Ban sorted by Ban, plus each
    opponent's final team ban value (all components), to compare the lane view with the ranking."""
    empty = DraftState(TOURNAMENT, "blue")
    tables: dict[str, RoleBanTable] = engine.lane_ban_tables(empty)
    team_value = {r["champ_id"]: r["value"] for r in engine.ban_breakdown(empty)}
    neutral = engine.cfg.lane_ban.neutral_band
    name = engine.data.name
    return [
        {
            "role": r,
            "role_label": ROLE_LABELS[r],
            "player_bracket": engine.team.brackets[r],
            "mu": round(tables[r].mu, 2),
            "rows": [
                {
                    "opp_id": row.opp,
                    "opp": name(row.opp),
                    "pr": round(100 * row.pick_rate, 1),
                    "resp": round(row.resp, 1),
                    "via_id": row.via,
                    "via": name(row.via) if row.via is not None else row.via_note,
                    "ban": round(row.ban, 2),
                    "band": _band(round(row.ban, 2), neutral),  # matches the displayed value
                    "degenerate": row.degenerate,
                    "team_ban_value": round(team_value[row.opp], 4) if row.opp in team_value else None,
                }
                for row in tables[r].rows
            ],
        }
        for r in ROLES
    ]


@router.get("/api/prep/lane-bans")
def prep_lane_bans(request: Request) -> dict:
    svc: Services = request.app.state.services
    return {
        "data_source": svc.data_source,
        "note": svc.note,
        "enemy_bracket": svc.engine.cfg.enemy_bracket,
        "roles": lane_ban_report(svc.engine),
    }

@router.get("/api/prep/matchup-tree")
def prep_matchup_tree(request: Request) -> dict:
    """Per role: each enemy archetype, our best answer, and the pool health card (docs/matchup-archetypes.md, Stage 7)."""
    svc: Services = request.app.state.services
    engine = svc.engine
    model = load_matchup_model(db_of(svc))
    plans = load_archetype_plans()

    def with_card(item: dict | None) -> dict | None:
        return None if item is None else {**item, "card": svc.catalog.card(item["champ"])}

    pooled = {}
    roles = []
    for role in ROLES:
        bracket = engine.team.brackets.get(role)
        if model is not None and bracket not in pooled:
            pooled[bracket] = pooled_stats(db_of(svc), model, bracket)
        expected = (lambda a, b, r=role: model.expected(r, a, b)) if model is not None else None
        rows = matchup_tree(engine, role, ARCHETYPES[role], plans.get(role), expected, pooled.get(bracket))
        for row in rows:
            row["champs"] = [with_card(c) for c in row["champs"]]
            row["best"] = with_card(row["best"])
        roles.append({"role": role, "role_label": ROLE_LABELS[role], "bracket": bracket,
                      "rows": rows, "health": pool_health(engine, role)})
    with db_of(svc).connect() as conn:
        counts = [{"patch": p, "matches": n} for p, n in conn.execute(text(
            "SELECT patch, COUNT(*) FROM crawled_matches GROUP BY patch ORDER BY patch"))]
    return {"roles": roles, "matches": counts}


@router.get("/api/learning")
def get_learning(request: Request) -> dict:
    return request.app.state.services.learning or {"running": False}

@router.post("/api/learning/run")
def post_learning(request: Request) -> dict:
    svc: Services = request.app.state.services
    return {"started": svc.learn(), **(svc.learning or {})}

@router.get("/api/team")
def get_team(request: Request) -> dict:
    svc: Services = request.app.state.services
    return team_summary(db_of(svc), svc.profiles())

@router.put("/api/team/style")
def put_team_style(request: Request, style: str | None = Body(None, embed=True)) -> dict:
    """The style our picks are pulled towards (comp coherence). Applied at once, except mid-draft."""
    svc: Services = request.app.state.services
    db = db_of(svc)
    try:
        set_hand_style(db, style or None)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    applied = not svc.in_draft()
    if applied:
        svc.reload()
    return {**team_summary(db, svc.profiles()), "applied": applied}

@router.get("/api/prep")
def get_prep(request: Request) -> dict:
    svc: Services = request.app.state.services
    return {"data_source": svc.data_source, **prep_sheet(svc.engine, svc.catalog.card)}

@router.get("/api/model-check")
def get_model_check(request: Request) -> dict:
    return model_check(db_of(request.app.state.services))
