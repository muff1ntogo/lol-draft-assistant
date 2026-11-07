"""Settings page: ban weights, engine weights and options (synergy settings: api/synergy_routes.py)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, HTTPException, Request

from draftassist.api.deps import db_of
from draftassist.data.settings_store import (
    save_ban_settings,
    save_engine_settings,
    settings_payload,
)
from draftassist.engine.model import BanWeights

if TYPE_CHECKING:
    from draftassist.api.main import Services

router = APIRouter()


# ---------- settings ----------
@router.get("/api/settings")
def get_settings(request: Request) -> dict:
    return settings_payload(db_of(request.app.state.services))

@router.put("/api/settings")
def put_settings(request: Request, ban_weights: dict = Body(...), ban_declared_intents: bool = Body(False),
                 alpha: float | None = Body(None), beta: float | None = Body(None),
                 comfort_games_cap: int | None = Body(None), damage_balance: bool | None = Body(None),
                 damage_penalty: float | None = Body(None), ban_comp_synergy: bool | None = Body(None),
                 comp_coherence: bool | None = Body(None), comp_weight: float | None = Body(None),
                 lane_weight: float | None = Body(None), role_weight_override: dict | None = Body(None),
                 blind_weights: dict | None = Body(None), gate_tau: float | None = Body(None),
                 execution_comfort: bool | None = Body(None), scout_enabled: bool | None = Body(None),
                 scout_weight: float | None = Body(None)) -> dict:
    svc: Services = request.app.state.services
    db = db_of(svc)
    try:
        weights = BanWeights(float(ban_weights["threat"]), float(ban_weights["matchup"]),
                             float(ban_weights["comp"]), float(ban_weights.get("own", BanWeights().own)))
        save_ban_settings(db, weights, ban_declared_intents)
        save_engine_settings(db, alpha=alpha, beta=beta, comfort_games_cap=comfort_games_cap,
                             damage_balance=damage_balance, damage_penalty=damage_penalty,
                             ban_comp_synergy=ban_comp_synergy, comp_coherence=comp_coherence,
                             comp_weight=comp_weight, lane_weight=lane_weight,
                             role_weight_override=role_weight_override, blind_weights=blind_weights,
                             gate_tau=gate_tau, execution_comfort=execution_comfort, scout_enabled=scout_enabled,
                             scout_weight=scout_weight)
        if comfort_games_cap is not None:
            from draftassist.learning.comfort import rescore

            rescore(db)  # stored comfort shown on the Pools page uses the new cap
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(422, f"invalid settings: {e}") from None
    svc.reload()
    return settings_payload(db)
