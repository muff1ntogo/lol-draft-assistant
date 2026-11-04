"""Archetypes per role (docs/matchup-archetypes.md, Stage 1): the vocabulary and the untagged fallback.

An archetype is written per champion and role in data/champion_tags.yaml, next to the 0-3 tags and sharing
their `reviewed` flag. Unlike tags, archetypes are never borrowed from another role: Karthus is a farmer in the
jungle and a mage bot in the bot lane. Champions without one get the nearest archetype from their Data Dragon
class, flagged `untagged`.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from draftassist.config import PROJECT_ROOT

PRIORS_FILE = PROJECT_ROOT / "data" / "archetype_matchups.yaml"
DEFAULT_PRIOR_PP = 0.5
TARGETS = {"lane": ("lane",), "game": ("game",), "both": ("lane", "game")}

ARCHETYPES: dict[str, tuple[str, ...]] = {
    "top": ("juggernaut", "diver", "skirmisher", "tank", "ranged"),
    "jungle": ("tempo_ganker", "farmer", "skirmisher", "tank", "assassin"),
    "mid": ("artillery", "control_mage", "battle_mage", "skirmisher", "assassin"),  # longest to shortest range
    "adc": ("scaler", "lane_bully", "poke", "skirmisher", "mage_bot"),
    "support": ("engage", "enchanter", "warden", "mage", "catcher"),  # proposed: no video yet
}
SOURCES = ("video",)  # no source = drafted by Claude or edited by hand
UNREVIEWED_WEIGHT = 0.5

# Data Dragon class -> nearest archetype. The first class Data Dragon lists is the champion's main one.
FALLBACK: dict[str, dict[str, str]] = {
    "top": {"Tank": "tank", "Fighter": "diver", "Assassin": "skirmisher", "Marksman": "ranged", "Mage": "ranged",
            "Support": "tank"},
    "jungle": {"Tank": "tank", "Fighter": "skirmisher", "Assassin": "assassin", "Marksman": "farmer",
               "Mage": "farmer", "Support": "tempo_ganker"},
    "mid": {"Tank": "battle_mage", "Fighter": "skirmisher", "Assassin": "assassin", "Marksman": "artillery",
            "Mage": "control_mage", "Support": "control_mage"},
    "adc": {"Tank": "skirmisher", "Fighter": "skirmisher", "Assassin": "skirmisher", "Marksman": "scaler",
            "Mage": "mage_bot", "Support": "poke"},
    "support": {"Tank": "engage", "Fighter": "engage", "Assassin": "catcher", "Marksman": "mage", "Mage": "mage",
                "Support": "enchanter"},
}
DEFAULT = {"top": "diver", "jungle": "skirmisher", "mid": "control_mage", "adc": "scaler", "support": "enchanter"}


@dataclass(frozen=True)
class Archetype:
    primary: str
    secondary: str | None = None
    reviewed: bool = False
    source: str | None = None
    untagged: bool = False  # from the Data Dragon fallback, not written for this champion and role

    @property
    def weight(self) -> float:
        return 1.0 if self.reviewed else UNREVIEWED_WEIGHT


def validate_archetypes(role: str, primary: str | None, secondary: str | None, source: str | None,
                        where: str) -> None:
    """Raises ValueError for anything but a role's own vocabulary."""
    words = ARCHETYPES.get(role, ())
    if primary is not None and primary not in words:
        raise ValueError(f"{where}: {primary!r} isn't a {role} archetype ({', '.join(words)})")
    if secondary is not None:
        if primary is None:
            raise ValueError(f"{where}: archetype2 needs an archetype")
        if secondary not in words:
            raise ValueError(f"{where}: {secondary!r} isn't a {role} archetype ({', '.join(words)})")
        if secondary == primary:
            raise ValueError(f"{where}: archetype2 repeats the archetype")
    if source is not None and source not in SOURCES:
        raise ValueError(f"{where}: unknown source {source!r}")


def load_archetype_priors(path: Path = PRIORS_FILE) -> dict[str, list[dict]]:
    """Who beats whom, as written (data/archetype_matchups.yaml): role -> entries, each with its size in pp."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, list[dict]] = {}
    for role, body in data.items():
        words = ARCHETYPES.get(role)
        if words is None:
            raise ValueError(f"{path.name}: unknown role {role!r}")
        size = float(body.get("size_pp", DEFAULT_PRIOR_PP))
        seen: dict[tuple[str, frozenset], float] = {}
        entries = []
        for e in body.get("priors") or []:
            row, col, direction, target = e.get("row"), e.get("col"), e.get("dir"), e.get("target")
            where = f"{path.name}: {role} {row} v {col}"
            if row not in words or col not in words or row == col:
                raise ValueError(f"{where}: needs two different {role} archetypes")
            if direction not in ("+", "="):
                raise ValueError(f"{where}: dir must be + or =, got {direction!r}")
            if target not in TARGETS:
                raise ValueError(f"{where}: target must be lane, game or both, got {target!r}")
            pp = 0.0 if direction == "=" else size * (0.5 if e.get("slight") else 1.0)
            for t in TARGETS[target]:
                key = (t, frozenset((row, col)))
                signed = pp if row < col else -pp  # the same pair written twice must agree, either way round
                if key in seen and seen[key] != signed:
                    raise ValueError(f"{where}: contradicts an earlier {t} entry")
                seen[key] = signed
            entries.append({"row": row, "col": col, "pp": pp, "target": target,
                            "source": e.get("source"), "reviewed": bool(e.get("reviewed", False))})
        out[role] = entries
    return out


def load_archetype_plans(path: Path = PRIORS_FILE) -> dict[str, dict[str, str]]:
    """role -> enemy archetype -> a one-line plan (data/archetype_matchups.yaml, editable)."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {}
    for role, body in data.items():
        plans = dict(body.get("plans") or {})
        wrong = set(plans) - set(ARCHETYPES.get(role, ()))
        if wrong:
            raise ValueError(f"{path.name}: {role} plans for unknown archetypes {sorted(wrong)}")
        out[role] = {str(k): str(v) for k, v in plans.items()}
    return out


def priors_for(priors: Mapping[str, list[dict]], role: str, target: str) -> dict[tuple[str, str], float]:
    """(a, b) -> pp favouring a, in both directions, for one target (lane or game)."""
    out: dict[tuple[str, str], float] = {}
    for e in priors.get(role, []):
        if target in TARGETS[e["target"]]:
            out[(e["row"], e["col"])] = e["pp"]
            out[(e["col"], e["row"])] = -e["pp"]
    return out


def fallback_archetype(role: str, classes: Sequence[str] | None) -> str:
    table = FALLBACK[role]
    for c in classes or ():
        if c in table:
            return table[c]
    return DEFAULT[role]


def resolve_archetype(entries: Mapping, champ, role: str, classes: Sequence[str] | None) -> Archetype:
    """The champion's archetype in this role, else the fallback from its Data Dragon classes (`untagged`)."""
    got = entries.get((champ, role))
    return got if got is not None else Archetype(fallback_archetype(role, classes), untagged=True)


def pool_gaps(pools: Mapping[str, Iterable[str]], raw: Mapping) -> list[tuple[str, str]]:
    """(role, champion key) for pool champions with no written archetype. `raw` = load_tags_yaml()."""
    gaps = []
    for role, keys in pools.items():
        for key in keys:
            e = raw.get(key, {}).get(role)
            if e is None or e.archetype is None:
                gaps.append((role, key))
    return gaps
