"""FastAPI app. Phase 2 adds the live WebSocket and the Live screen; for now it serves the
Prep page's lane ban tables.

Run: `uv run uvicorn draftassist.api.main:app --reload`, then GET /api/prep/lane-bans
"""
from __future__ import annotations

from fastapi import FastAPI

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.lane_ban import RoleBanTable
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import DraftState

FIXTURE_NOTE = "matchups table is empty until Phase 3; showing synthetic fixture data"


def _band(ban: float, neutral: float) -> str:
    if ban > neutral:
        return "positive"
    if ban < -neutral:
        return "negative"
    return "neutral"


def lane_ban_report(engine: DraftEngine) -> list[dict]:
    """The Prep page's per-role tables: mu, then Opp / PR / Resp / via / Ban sorted by Ban."""
    tables: dict[str, RoleBanTable] = engine.lane_ban_tables(DraftState(TOURNAMENT, "blue"))
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
                }
                for row in tables[r].rows
            ],
        }
        for r in ROLES
    ]


def create_app(engine: DraftEngine, data_source: str, note: str | None = None) -> FastAPI:
    app = FastAPI(title="Draft Assistant")

    @app.get("/api/prep/lane-bans")
    def prep_lane_bans() -> dict:
        return {
            "data_source": data_source,
            "note": note,
            "enemy_bracket": engine.cfg.enemy_bracket,
            "roles": lane_ban_report(engine),
        }

    return app


def _default_app() -> FastAPI:
    data, team = synthetic_data()
    return create_app(DraftEngine(data, team), "fixture", FIXTURE_NOTE)


app = _default_app()
