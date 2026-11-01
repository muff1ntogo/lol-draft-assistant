"""User settings kept in the database (Settings page): ban value weights and the declared-intents flag."""
from __future__ import annotations

import json

from sqlalchemy import Engine
from sqlmodel import Session

from draftassist.db.models import AppSetting
from draftassist.engine.model import BanWeights, EngineConfig

BAN_KEY = "ban"


def get_ban_settings(db: Engine) -> tuple[BanWeights, bool]:
    with Session(db) as s:
        row = s.get(AppSetting, BAN_KEY)
    if row is None:
        return BanWeights(), EngineConfig().ban_declared_intents
    v = json.loads(row.value)
    w = v.get("weights", {})
    defaults = BanWeights()
    return (BanWeights(float(w.get("threat", defaults.threat)), float(w.get("matchup", defaults.matchup)),
                       float(w.get("comp", defaults.comp))), bool(v.get("ban_declared_intents", False)))


def save_ban_settings(db: Engine, weights: BanWeights, ban_declared_intents: bool) -> None:
    for name, value in (("threat", weights.threat), ("matchup", weights.matchup), ("comp", weights.comp)):
        if not 0 <= value <= 10:
            raise ValueError(f"weight {name} must be between 0 and 10")
    value = json.dumps({"weights": {"threat": weights.threat, "matchup": weights.matchup, "comp": weights.comp},
                        "ban_declared_intents": ban_declared_intents})
    with Session(db) as s:
        row = s.get(AppSetting, BAN_KEY) or AppSetting(key=BAN_KEY, value=value)
        row.value = value
        s.add(row)
        s.commit()


ENGINE_KEY = "engine"


def get_alpha_beta(db: Engine) -> tuple[float, float]:
    with Session(db) as s:
        row = s.get(AppSetting, ENGINE_KEY)
    v = json.loads(row.value) if row else {}
    d = EngineConfig()
    return float(v.get("alpha", d.alpha)), float(v.get("beta", d.beta))


def save_alpha_beta(db: Engine, alpha: float, beta: float) -> None:
    for name, value in (("alpha", alpha), ("beta", beta)):
        if not 0 <= value <= 5:
            raise ValueError(f"{name} must be between 0 and 5")
    with Session(db) as s:
        row = s.get(AppSetting, ENGINE_KEY) or AppSetting(key=ENGINE_KEY, value="{}")
        row.value = json.dumps({"alpha": alpha, "beta": beta})
        s.add(row)
        s.commit()


def engine_config(db: Engine) -> EngineConfig:
    weights, declared = get_ban_settings(db)
    alpha, beta = get_alpha_beta(db)
    return EngineConfig(alpha=alpha, beta=beta, ban_weights=weights, ban_declared_intents=declared)


def settings_payload(db: Engine) -> dict:
    weights, declared = get_ban_settings(db)
    on_slider = abs(weights.threat + weights.matchup - 2.5) < 1e-9
    alpha, beta = get_alpha_beta(db)
    return {"ban_weights": {"threat": weights.threat, "matchup": weights.matchup, "comp": weights.comp},
            "slider": round(weights.matchup / 2.5, 4) if on_slider else None,
            "ban_declared_intents": declared, "alpha": alpha, "beta": beta}
