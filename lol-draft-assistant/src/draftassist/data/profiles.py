"""Champion profiles for comp type and team-fight style (docs/comp-style.md).

- Timing: power curve per champion and role from champ_role_stats' short/long game counts (every
  bracket on the patch pooled).
- Style: the user's review (champion_styles table) over the seed file champion_styles.json; champions
  missing from the seed (e.g. a new release) fall back to their Data Dragon classes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, text
from sqlmodel import Session, select

from draftassist.db.models import Champion, ChampionStyle
from draftassist.engine.comp import EMPTY_PROFILES, ChampionProfiles, ChampStyle, TimingCurve, power_curve

__all__ = ["EMPTY_PROFILES", "ChampionProfiles", "load_profiles", "load_seed", "reset_style", "save_style",
           "style_from_classes"]

SEED_FILE = Path(__file__).with_name("champion_styles.json")
Weights = tuple[float, float, float]

# Data Dragon class -> (front to back, dive, poke), front-line
_CLASS_STYLE: dict[str, tuple[Weights, bool]] = {
    "Tank": ((0.5, 0.5, 0.0), True),
    "Fighter": ((0.4, 0.6, 0.0), True),
    "Assassin": ((0.0, 1.0, 0.0), False),
    "Mage": ((0.4, 0.0, 0.6), False),
    "Marksman": ((0.8, 0.0, 0.2), False),
    "Support": ((1.0, 0.0, 0.0), False),
}


@dataclass(frozen=True)
class Seed:
    champions: Mapping[str, tuple[Weights, bool]]  # Data Dragon key -> (weights, front-line)
    unsure: frozenset[str]


@lru_cache(maxsize=1)
def load_seed(path: Path = SEED_FILE) -> Seed:
    raw = json.loads(path.read_text(encoding="utf-8"))
    champs = {k: ((float(v[0]), float(v[1]), float(v[2])), bool(v[3])) for k, v in raw["champions"].items()}
    return Seed(champs, frozenset(raw.get("unsure", ())))


def style_from_classes(tags: list[str]) -> tuple[Weights, bool]:
    known = [_CLASS_STYLE[t] for t in tags if t in _CLASS_STYLE]
    if not known:
        return (1 / 3, 1 / 3, 1 / 3), False
    weights = tuple(sum(w[i] for w, _ in known) / len(known) for i in range(3))
    return weights, any(t in ("Tank", "Fighter") for t in tags)


def _resolve(champ: Champion, override: ChampionStyle | None, seed: Seed) -> ChampStyle:
    in_seed = seed.champions.get(champ.key)
    if in_seed is not None:
        weights, frontline = in_seed
        base = ChampStyle(weights, frontline, "seed", False, champ.key in seed.unsure)
    else:
        weights, frontline = style_from_classes(champ.tags or [])
        base = ChampStyle(weights, frontline, "classes", False, True)
    if override is None:
        return base
    if override.front_to_back is None:  # the seed confirmed as is
        return ChampStyle(base.weights, base.frontline, base.source, True, base.unsure)
    weights = (override.front_to_back, override.dive, override.poke)
    frontline = base.frontline if override.frontline is None else override.frontline
    return ChampStyle(weights, frontline, "user", True, base.unsure)


def load_profiles(db: Engine, patch: str | None) -> ChampionProfiles:
    seed = load_seed()
    with Session(db) as s:
        champs = s.exec(select(Champion)).all()
        overrides = {o.champ_id: o for o in s.exec(select(ChampionStyle)).all()}
    styles = {c.champ_id: _resolve(c, overrides.get(c.champ_id), seed) for c in champs}

    curves = {}
    if patch is not None:
        with db.connect() as conn:
            rows = conn.execute(text(
                "SELECT champ_id, role, SUM(games), SUM(wins), SUM(games_short), SUM(wins_short), "
                "SUM(games_long), SUM(wins_long) FROM champ_role_stats "
                "WHERE patch = :patch AND games_short IS NOT NULL GROUP BY champ_id, role"), {"patch": patch}).all()
        for c, r, games, wins, gs, ws, gl, wl in rows:
            p = (wins + 25) / (games + 50)  # the champion's own win rate, lightly shrunk
            curves[(c, r)] = TimingCurve(power_curve(ws, gs, wl, gl, p), gs + gl)
    return ChampionProfiles(styles, curves)


def _validate(weights: Weights) -> None:
    if len(weights) != 3 or any(not 0 <= w <= 1 for w in weights):
        raise ValueError("each style weight must be between 0 and 1")
    if abs(sum(weights) - 1) > 0.01:
        raise ValueError(f"style weights must add up to 1 (got {sum(weights):.2f})")


def save_style(db: Engine, champ_id: int, weights: Weights | None, frontline: bool | None) -> None:
    """The user's review. weights None = confirm the seed as is."""
    if weights is not None:
        _validate(weights)
    with Session(db) as s:
        row = s.get(ChampionStyle, champ_id) or ChampionStyle(champ_id=champ_id, updated_at=datetime.now(timezone.utc))
        row.front_to_back, row.dive, row.poke = weights if weights is not None else (None, None, None)
        row.frontline = frontline
        row.updated_at = datetime.now(timezone.utc)
        s.add(row)
        s.commit()


def reset_style(db: Engine, champ_id: int) -> None:
    with Session(db) as s:
        row = s.get(ChampionStyle, champ_id)
        if row is not None:
            s.delete(row)
            s.commit()
