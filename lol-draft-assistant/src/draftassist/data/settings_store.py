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
                       float(w.get("comp", defaults.comp)), float(w.get("own", defaults.own))),
            bool(v.get("ban_declared_intents", False)))


def save_ban_settings(db: Engine, weights: BanWeights, ban_declared_intents: bool) -> None:
    for name, value in (("threat", weights.threat), ("matchup", weights.matchup), ("comp", weights.comp),
                        ("own", weights.own)):
        if not 0 <= value <= 10:
            raise ValueError(f"weight {name} must be between 0 and 10")
    value = json.dumps({"weights": {"threat": weights.threat, "matchup": weights.matchup, "comp": weights.comp,
                                    "own": weights.own},
                        "ban_declared_intents": ban_declared_intents})
    with Session(db) as s:
        row = s.get(AppSetting, BAN_KEY) or AppSetting(key=BAN_KEY, value=value)
        row.value = value
        s.add(row)
        s.commit()


ENGINE_KEY = "engine"


def get_engine_settings(db: Engine) -> dict:
    """alpha (meta), beta (comfort) and the comfort games cap."""
    from draftassist.learning.comfort import DEFAULT_GAMES_CAP

    with Session(db) as s:
        row = s.get(AppSetting, ENGINE_KEY)
    v = json.loads(row.value) if row else {}
    d = EngineConfig()
    cap = v.get("comfort_games_cap", DEFAULT_GAMES_CAP)
    return {"alpha": float(v.get("alpha", d.alpha)), "beta": float(v.get("beta", d.beta)),
            "comfort_games_cap": int(cap) if cap else None}


def get_alpha_beta(db: Engine) -> tuple[float, float]:
    s = get_engine_settings(db)
    return s["alpha"], s["beta"]


def save_engine_settings(db: Engine, alpha: float | None = None, beta: float | None = None,
                         comfort_games_cap: int | None = None) -> None:
    """Update only the values given; the others keep what was saved."""
    current = get_engine_settings(db)
    for name, value, hi in (("alpha", alpha, 5), ("beta", beta, 5)):
        if value is not None and not 0 <= value <= hi:
            raise ValueError(f"{name} must be between 0 and {hi}")
    if comfort_games_cap is not None and not 1 <= comfort_games_cap <= 200:
        raise ValueError("comfort games cap must be between 1 and 200")
    merged = {"alpha": current["alpha"] if alpha is None else alpha,
              "beta": current["beta"] if beta is None else beta,
              "comfort_games_cap": current["comfort_games_cap"] if comfort_games_cap is None else comfort_games_cap}
    with Session(db) as s:
        row = s.get(AppSetting, ENGINE_KEY) or AppSetting(key=ENGINE_KEY, value="{}")
        row.value = json.dumps(merged)
        s.add(row)
        s.commit()


def save_alpha_beta(db: Engine, alpha: float, beta: float) -> None:
    save_engine_settings(db, alpha=alpha, beta=beta)


def engine_config(db: Engine) -> EngineConfig:
    weights, declared = get_ban_settings(db)
    alpha, beta = get_alpha_beta(db)
    return EngineConfig(alpha=alpha, beta=beta, ban_weights=weights, ban_declared_intents=declared)


def settings_payload(db: Engine) -> dict:
    weights, declared = get_ban_settings(db)
    on_slider = abs(weights.threat + weights.matchup - 2.5) < 1e-9
    engine = get_engine_settings(db)
    return {"ban_weights": {"threat": weights.threat, "matchup": weights.matchup, "comp": weights.comp,
                            "own": weights.own},
            "slider": round(weights.matchup / 2.5, 4) if on_slider else None,
            "ban_declared_intents": declared, "alpha": engine["alpha"], "beta": engine["beta"],
            "comfort_games_cap": engine["comfort_games_cap"]}
