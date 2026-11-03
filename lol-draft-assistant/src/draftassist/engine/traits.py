"""Champion traits for synergy v2 (docs/synergy-v2.md, Stage 1). Pure, no I/O.

Two sources per champion and role:
- measured traits (data/traits.py): percentiles within the role, 0-1, or None when the source is missing;
- judgement tags rated 0-3 (data/champion_tags.yaml), scaled to 0-1. Unreviewed tags count at half weight:
  they blend halfway towards the role's average for that tag (0.5 * own + 0.5 * role average), so an
  unreviewed tag is uncertain in both directions and reviewing sharpens it without flipping rankings.

`ChampionTraits.intensity(champ, role, term)` is what every model reads. A term is a trait name, a tag, a
derived term (`ranged_long`, `power_curve_late`, `power_curve_early`) or `not <term>` (1 - intensity).
It returns None when nothing is known (no tag, no data); the models then treat the feature as absent.
"""
from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass

from draftassist.engine.mathx import logit

TAGS = ("engage", "disengage", "poke", "dive", "pick", "splitpush", "enchanter", "hypercarry")
TAG_MAX = 3
UNREVIEWED_WEIGHT = 0.5

# Power curve: win rate in three game-length buckets (< 25, 25-32, > 32 min), each shrunk towards the
# champion's own win rate (k games, as in engine/comp.py), regressed on bucket index -1 / 0 / +1.
CURVE_K = 50
CURVE_MIN_GAMES = 50
RANGED_MIN = 300  # Data Dragon attack range above this = ranged
RANGED_LONG = (500, 650)  # ranged_long: 0 at 500 (Lucian, Samira), 1 at 650 (Caitlyn)
DERIVED_TERMS = ("ranged_long", "power_curve_late", "power_curve_early")


def power_curve_slope(buckets: Sequence[tuple[float, float]], k: float = CURVE_K,
                      min_games: float = CURVE_MIN_GAMES) -> float | None:
    """Slope of logit(win rate) across the (wins, games) buckets short / mid / long, weighted by games.
    Negative = wins short games (early), positive = scaling. None with fewer than `min_games` games."""
    games = sum(g for _, g in buckets)
    if games < min_games:
        return None
    p = sum(w for w, _ in buckets) / games
    xs = [i - (len(buckets) - 1) / 2 for i in range(len(buckets))]
    ys = [logit((w + k * p) / (g + k)) for w, g in buckets]
    ws = [g for _, g in buckets]
    sw = sum(ws)
    mx = sum(w * x for w, x in zip(ws, xs)) / sw
    my = sum(w * y for w, y in zip(ws, ys)) / sw
    sxx = sum(w * (x - mx) ** 2 for w, x in zip(ws, xs))
    if sxx <= 0:
        return None
    return sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys)) / sxx


def percentiles(values: Mapping[Hashable, float | None]) -> dict[Hashable, float | None]:
    """Rank -> 0..1 (lowest 0, highest 1; ties share their mean rank; a single value is 0.5). None stays None."""
    known = sorted((v, k) for k, v in values.items() if v is not None)
    out: dict[Hashable, float | None] = {k: None for k, v in values.items() if v is None}
    n = len(known)
    if n == 1:
        out[known[0][1]] = 0.5
        return out
    i = 0
    while i < n:
        j = i
        while j + 1 < n and known[j + 1][0] == known[i][0]:
            j += 1
        rank = (i + j) / 2 / (n - 1)
        for _, k in known[i:j + 1]:
            out[k] = rank
        i = j + 1
    return out


def ranged(attack_range: float | None) -> int | None:
    return None if attack_range is None else int(attack_range > RANGED_MIN)


def ranged_long(attack_range: float | None) -> float | None:
    if attack_range is None:
        return None
    lo, hi = RANGED_LONG
    return min(max((attack_range - lo) / (hi - lo), 0.0), 1.0)


@dataclass(frozen=True)
class TagEntry:
    values: Mapping[str, int]  # tag -> 0..3, every tag present
    reviewed: bool = False

    @property
    def weight(self) -> float:
        return 1.0 if self.reviewed else UNREVIEWED_WEIGHT

    def raw(self, tag: str) -> float:
        return self.values.get(tag, 0) / TAG_MAX

    def scaled(self, tag: str, role_average: float) -> float:
        """Reviewed: its own value. Unreviewed: weight * own + (1 - weight) * the role's average."""
        return self.weight * self.raw(tag) + (1 - self.weight) * role_average


Key = tuple[int, str]  # (champ, role)


@dataclass(frozen=True)
class ChampionTraits:
    measured: Mapping[Key, Mapping[str, float | None]]  # (champ, role) -> trait -> value
    tags: Mapping[Key, TagEntry]
    attack_range: Mapping[int, float | None]

    def __post_init__(self):
        by_champ: dict[int, TagEntry] = {}
        for (c, _), entry in sorted(self.tags.items(), key=lambda kv: kv[0][1], reverse=True):
            by_champ[c] = entry  # alphabetically first role wins
        object.__setattr__(self, "_by_champ", by_champ)
        sums: dict[tuple[str, str], list[float]] = {}
        for (_, role), entry in self.tags.items():
            for tag in TAGS:
                sums.setdefault((role, tag), []).append(entry.raw(tag))
        object.__setattr__(self, "_role_avg", {k: sum(v) / len(v) for k, v in sums.items()})

    def role_average(self, role: str, tag: str) -> float:
        """Mean of the tag (0-1, as written, reviewed or not) over the role's tagged champions; 0 if none."""
        return self._role_avg.get((role, tag), 0.0)

    def tag_entry(self, champ: int, role: str) -> TagEntry | None:
        """The champion's tags in this role, else its tags in another role (tags are mostly about the
        champion), else None."""
        e = self.tags.get((champ, role))
        if e is not None:
            return e
        return self._by_champ.get(champ)

    def untagged(self, champ: int) -> bool:
        return champ not in self._by_champ

    def _range(self, champ: int, role: str) -> float | None:
        r = self.attack_range.get(champ)
        if r is None:
            m = self.measured.get((champ, role)) or {}
            return None if m.get("ranged") is None else (600.0 if m["ranged"] else 125.0)
        return r

    def intensity(self, champ: int, role: str, term: str) -> float | None:
        term = term.strip()
        if term.startswith("not "):
            v = self.intensity(champ, role, term[4:])
            return None if v is None else 1.0 - v
        if term in TAGS:
            e = self.tag_entry(champ, role)
            return None if e is None else e.scaled(term, self.role_average(role, term))
        m = self.measured.get((champ, role)) or {}
        if term == "ranged_long":
            return ranged_long(self._range(champ, role))
        if term == "ranged":
            r = self._range(champ, role)
            return None if r is None else float(ranged(r))
        if term in ("power_curve_late", "power_curve_early"):
            v = m.get("power_curve")
            return None if v is None else (v if term == "power_curve_late" else 1.0 - v)
        if term not in m and term not in KNOWN_TERMS:
            raise KeyError(f"unknown trait term: {term}")
        return m.get(term)


KNOWN_TERMS = frozenset(TAGS) | frozenset(DERIVED_TERMS) | {
    "cc", "frontline", "peel", "dmg_phys", "dmg_magic", "dmg_true", "power_curve", "lane_pressure", "ranged",
    "cc_all", "frontline_all", "peel_all"}

EMPTY_TRAITS = ChampionTraits({}, {}, {})


def validate_term(expr: str) -> str:
    """A rule term: one or more factors joined by `*`, each a known term, optionally `not <term>`.
    Returns the normalised expression; raises ValueError for an unknown term."""
    factors = [f.strip() for f in str(expr).split("*")]
    for f in factors:
        name = f[4:].strip() if f.startswith("not ") else f
        if name not in KNOWN_TERMS:
            raise ValueError(f"unknown trait term: {name!r}")
    return " * ".join(factors)


def term_value(traits: ChampionTraits, champ: int, role: str, expr: str) -> float | None:
    """Product of the factors' intensities; None if any factor is unknown."""
    out = 1.0
    for f in expr.split("*"):
        v = traits.intensity(champ, role, f.strip())
        if v is None:
            return None
        out *= v
    return out


def term_role_averages(traits: ChampionTraits, role: str, exprs) -> dict[str, float]:
    """Mean of each term over the role's known champions (measured games as weights, tags-only = 1).
    0 when nothing in the role is known. Used to fill unknown values in team features, so a missing
    trait reads as typical (and cancels against the average comp) rather than as 0."""
    champs = {c for c, r in traits.measured if r == role} | {c for c, r in traits.tags if r == role}
    out = {}
    for e in exprs:
        num = den = 0.0
        for c in champs:
            v = term_value(traits, c, role, e)
            if v is None:
                continue
            w = float((traits.measured.get((c, role)) or {}).get("games") or 1)
            num, den = num + w * v, den + w
        out[e] = num / den if den else 0.0
    return out
