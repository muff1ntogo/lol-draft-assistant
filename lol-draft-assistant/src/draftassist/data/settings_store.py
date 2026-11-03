"""User settings kept in the database (Settings page): ban value weights, the declared-intents flag and the
engine settings (alpha, beta, damage balance, comp coherence...)."""
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
    """alpha (meta), beta (comfort), the comfort games cap, damage balance and the ban synergy flag."""
    from draftassist.learning.comfort import DEFAULT_GAMES_CAP

    with Session(db) as s:
        row = s.get(AppSetting, ENGINE_KEY)
    v = json.loads(row.value) if row else {}
    d = EngineConfig()
    cap = v.get("comfort_games_cap", DEFAULT_GAMES_CAP)
    return {"alpha": float(v.get("alpha", d.alpha)), "beta": float(v.get("beta", d.beta)),
            "comfort_games_cap": int(cap) if cap else None,
            "damage_balance": bool(v.get("damage_balance", d.damage_balance)),
            "damage_penalty": float(v.get("damage_penalty", d.damage_penalty)),
            "ban_comp_synergy": bool(v.get("ban_comp_synergy", d.ban_comp_synergy)),
            "comp_coherence": bool(v.get("comp_coherence", d.comp_coherence)),
            "comp_weight": float(v.get("comp_weight", d.comp_weight))}


def get_alpha_beta(db: Engine) -> tuple[float, float]:
    s = get_engine_settings(db)
    return s["alpha"], s["beta"]


def save_engine_settings(db: Engine, alpha: float | None = None, beta: float | None = None,
                         comfort_games_cap: int | None = None, damage_balance: bool | None = None,
                         damage_penalty: float | None = None, ban_comp_synergy: bool | None = None,
                         comp_coherence: bool | None = None, comp_weight: float | None = None) -> None:
    """Update only the values given; the others keep what was saved."""
    current = get_engine_settings(db)
    for name, value, hi in (("alpha", alpha, 5), ("beta", beta, 5)):
        if value is not None and not 0 <= value <= hi:
            raise ValueError(f"{name} must be between 0 and {hi}")
    if comfort_games_cap is not None and not 1 <= comfort_games_cap <= 200:
        raise ValueError("comfort games cap must be between 1 and 200")
    if damage_penalty is not None and not -1 <= damage_penalty <= 0:
        raise ValueError("damage penalty must be between -1 and 0 (log-odds)")
    if comp_weight is not None and not 0 <= comp_weight <= 1:
        raise ValueError("comp coherence weight must be between 0 and 1 (log-odds)")
    merged = {"alpha": current["alpha"] if alpha is None else alpha,
              "beta": current["beta"] if beta is None else beta,
              "comfort_games_cap": current["comfort_games_cap"] if comfort_games_cap is None else comfort_games_cap}
    for name, value in (("damage_balance", damage_balance), ("damage_penalty", damage_penalty),
                        ("ban_comp_synergy", ban_comp_synergy), ("comp_coherence", comp_coherence),
                        ("comp_weight", comp_weight)):
        merged[name] = current[name] if value is None else value
    with Session(db) as s:
        row = s.get(AppSetting, ENGINE_KEY) or AppSetting(key=ENGINE_KEY, value="{}")
        row.value = json.dumps(merged)
        s.add(row)
        s.commit()


def save_alpha_beta(db: Engine, alpha: float, beta: float) -> None:
    save_engine_settings(db, alpha=alpha, beta=beta)


def engine_config(db: Engine) -> EngineConfig:
    from draftassist.learning.style import get_hand_style

    weights, declared = get_ban_settings(db)
    e = get_engine_settings(db)
    return EngineConfig(alpha=e["alpha"], beta=e["beta"], ban_weights=weights, ban_declared_intents=declared,
                        damage_balance=e["damage_balance"], damage_penalty=e["damage_penalty"],
                        ban_comp_synergy=e["ban_comp_synergy"], comp_coherence=e["comp_coherence"],
                        comp_weight=e["comp_weight"], comp_target=get_hand_style(db))


def settings_payload(db: Engine) -> dict:
    weights, declared = get_ban_settings(db)
    on_slider = abs(weights.threat + weights.matchup - 2.5) < 1e-9
    engine = get_engine_settings(db)
    return {"ban_weights": {"threat": weights.threat, "matchup": weights.matchup, "comp": weights.comp,
                            "own": weights.own},
            "slider": round(weights.matchup / 2.5, 4) if on_slider else None,
            "ban_declared_intents": declared, "alpha": engine["alpha"], "beta": engine["beta"],
            "comfort_games_cap": engine["comfort_games_cap"], "damage_balance": engine["damage_balance"],
            "damage_penalty": engine["damage_penalty"], "ban_comp_synergy": engine["ban_comp_synergy"],
            "comp_coherence": engine["comp_coherence"], "comp_weight": engine["comp_weight"],
            "synergy_v2": get_synergy_v2_settings(db)}


SYNERGY_KEY = "synergy"


def get_synergy_bracket(db: Engine) -> str:
    """Bracket the synergy score is read from: "all" (every crawled tier pooled) or one tier."""
    with Session(db) as s:
        row = s.get(AppSetting, SYNERGY_KEY)
    return json.loads(row.value).get("bracket", "all") if row else "all"


def save_synergy_bracket(db: Engine, bracket: str) -> None:
    from draftassist.data.crawler import LEAGUE_TIERS

    if bracket != "all" and bracket not in LEAGUE_TIERS:
        raise ValueError(f"synergy bracket must be 'all' or one of {', '.join(LEAGUE_TIERS)}")
    with Session(db) as s:
        row = s.get(AppSetting, SYNERGY_KEY) or AppSetting(key=SYNERGY_KEY, value="{}")
        row.value = json.dumps({"bracket": bracket})
        s.add(row)
        s.commit()


SYNERGY_V2_KEY = "synergy_v2"
SYNERGY_V2_DEFAULTS = {"prior_sd_pp": 1.0, "w_stat": 0.50, "w_style": 0.25, "w_team": 0.25, "conf_k": 200.0,
                       "ban_teamfight_d": 1.0}
_SYNERGY_V2_RANGES = {"prior_sd_pp": (0.1, 10), "w_stat": (0, 5), "w_style": (0, 5), "w_team": (0, 5),
                      "conf_k": (1, 5000), "ban_teamfight_d": (0, 10)}


def get_synergy_v2_settings(db: Engine) -> dict:
    """Synergy v2 settings (docs/synergy-v2.md): prior SD of the style and team fits (pp), the Stat / Style /
    Team weights of Overall, the confidence games k, and d (Win teamfights in the Combined ban list)."""
    with Session(db) as s:
        row = s.get(AppSetting, SYNERGY_V2_KEY)
    v = json.loads(row.value) if row else {}
    return {k: float(v.get(k, d)) for k, d in SYNERGY_V2_DEFAULTS.items()}


def save_synergy_v2_settings(db: Engine, **values: float) -> None:
    current = get_synergy_v2_settings(db)
    for k, v in values.items():
        if k not in SYNERGY_V2_DEFAULTS:
            raise ValueError(f"unknown synergy setting {k}")
        lo, hi = _SYNERGY_V2_RANGES[k]
        if not lo <= float(v) <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
        current[k] = float(v)
    if current["w_stat"] + current["w_style"] <= 0:
        raise ValueError("Stat and Style weights can't both be 0")
    with Session(db) as s:
        row = s.get(AppSetting, SYNERGY_V2_KEY) or AppSetting(key=SYNERGY_V2_KEY, value="{}")
        row.value = json.dumps(current)
        s.add(row)
        s.commit()


def synergy_weights(db: Engine):
    from draftassist.engine.synergy_v2 import SynergyWeights

    v = get_synergy_v2_settings(db)
    return SynergyWeights(v["w_stat"], v["w_style"], v["w_team"], v["conf_k"])
