"""Champion profiles for comp type and fight style (docs/comp-style.md, merged into docs/synergy-v2.md).

- Timing: power curve per champion and role from champ_role_stats' short/long game counts (every
  bracket on the patch pooled).
- Style: shares over the five comp identities (engage, poke, pick, protect, split) from the champion's
  judgement tags (data/champion_tags.yaml, reviewed on the Tags page) and its measured peel, per role:
      engage = engage + 1/2 dive, poke, pick, protect = enchanter + 1/2 disengage + 1/2 peel, split
  Tags are read as written (0-3 -> 0-1), reviewed or not: the half weight of unreviewed tags is for the fitted
  models, not for labels. Untagged champions (e.g. a new release) fall back to their Data Dragon classes.
- Front-line: measured frontline (percentile across every role) >= FRONTLINE_PCT, else the Tank class.

The comp-style seed file and its review table (champion_styles) were the old source; the table is kept in the
database but no longer read.
"""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Engine, text
from sqlmodel import Session, select

from draftassist.db.models import Champion
from draftassist.engine.comp import (
    EMPTY_PROFILES,
    IDENTITY_TERMS,
    STYLES,
    ChampionProfiles,
    ChampStyle,
    TimingCurve,
    power_curve,
)
from draftassist.engine.traits import TAGS, ChampionTraits

__all__ = ["EMPTY_PROFILES", "ChampionProfiles", "load_profiles", "style_from_classes", "style_from_traits"]

FRONTLINE_PCT = 0.70
Weights = tuple[float, ...]

# Data Dragon class -> identity shares (engage, poke, pick, protect, split), for untagged champions
_CLASS_STYLE: dict[str, Weights] = {
    "Tank": (0.7, 0.0, 0.0, 0.3, 0.0),
    "Fighter": (0.5, 0.0, 0.0, 0.0, 0.5),
    "Assassin": (0.3, 0.0, 0.7, 0.0, 0.0),
    "Mage": (0.0, 0.6, 0.4, 0.0, 0.0),
    "Marksman": (0.0, 0.4, 0.0, 0.6, 0.0),
    "Support": (0.0, 0.0, 0.0, 1.0, 0.0),
}


def style_from_classes(tags: list[str]) -> tuple[Weights, bool]:
    known = [_CLASS_STYLE[t] for t in tags if t in _CLASS_STYLE]
    if not known:
        return tuple(1 / len(STYLES) for _ in STYLES), False
    weights = tuple(sum(w[i] for w in known) / len(known) for i in range(len(STYLES)))
    return weights, "Tank" in tags


def style_from_traits(traits: ChampionTraits, champ: int, role: str) -> tuple[Weights, bool] | None:
    """Identity shares from the champion's tags (as written) and measured peel in `role`; None if untagged."""
    entry = traits.tag_entry(champ, role)
    if entry is None:
        return None
    measured = traits.measured.get((champ, role)) or {}

    def value(term: str) -> float:
        if term in TAGS:
            return entry.raw(term)
        v = measured.get(term)
        return 0.0 if v is None else float(v)

    raw = [sum(w * value(t) for t, w in IDENTITY_TERMS[k]) for k in STYLES]
    total = sum(raw)
    if total <= 0:
        return None
    frontline = (measured.get("frontline_all") or 0.0) >= FRONTLINE_PCT
    return tuple(r / total for r in raw), frontline


def load_profiles(db: Engine, patch: str | None, traits: ChampionTraits | None = None) -> ChampionProfiles:
    from draftassist.data.traits import load_traits

    traits = traits if traits is not None else load_traits(db)
    with Session(db) as s:
        champs = s.exec(select(Champion)).all()
    styles: dict[int, ChampStyle] = {}
    role_styles: dict[tuple[int, str], ChampStyle] = {}
    tagged_roles: dict[int, list[str]] = {}
    for c, r in traits.tags:
        tagged_roles.setdefault(c, []).append(r)
    for champ in champs:
        cid = champ.champ_id
        roles = list(dict.fromkeys([*(champ.roles or []), *sorted(tagged_roles.get(cid, []))]))
        frontline_class = "Tank" in (champ.tags or [])
        per_role = {}
        for r in roles:
            got = style_from_traits(traits, cid, r)
            if got is not None:
                weights, frontline = got
                reviewed = traits.tag_entry(cid, r).reviewed
                per_role[r] = ChampStyle(weights, frontline or frontline_class, "tags", reviewed, False)
        if per_role:
            main = next(r for r in roles if r in per_role)
            styles[cid] = per_role[main]
            role_styles |= {(cid, r): st for r, st in per_role.items()}
        else:
            weights, frontline = style_from_classes(champ.tags or [])
            styles[cid] = ChampStyle(weights, frontline, "classes", False, True)

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
    return ChampionProfiles(styles, curves, role_styles)


def profiles_from_traits(traits: ChampionTraits, class_tags: Mapping[int, list[str]] | None = None,
                         curves: Mapping[tuple[int, str], TimingCurve] | None = None) -> ChampionProfiles:
    """Profiles without a database (tests, fixtures): styles from the traits' tags."""
    styles, role_styles = {}, {}
    for (c, r) in sorted(traits.tags):
        got = style_from_traits(traits, c, r)
        if got is None:
            continue
        st = ChampStyle(got[0], got[1], "tags", traits.tags[(c, r)].reviewed, False)
        role_styles[(c, r)] = st
        styles.setdefault(c, st)
    for c, tags in (class_tags or {}).items():
        if c not in styles:
            weights, frontline = style_from_classes(tags)
            styles[c] = ChampStyle(weights, frontline, "classes", False, True)
    return ChampionProfiles(styles, dict(curves or {}), role_styles)
