"""Synergy v2 components and aggregates (docs/synergy-v2.md, Stage 3). Pure, no I/O.

All in log-odds (shown as pp = 25 x log-odds):
- Stat(A, B): the pair's normalised win-rate synergy s (engine/synergy.py), already shrunk towards 0.
- Style(A, B): the fitted pair style rules (engine/style.py); core pairs only.
- Team(comp): the team model (engine/team.py), relative to the average comp; missing roles are filled
  with an average champion of the role, so a partial comp has a well-defined Team.

Per core pair (TOP-JG, JG-MID, ADC-SUP), with confidence c = games / (games + 200):
    Lane = [ws c Stat + (wy + ws (1 - c)) Style] / (ws + wy)
Whole comp:
    Overall = sum_core [ws c Stat + (wy + ws (1 - c)) Style] + sum_other ws Stat + wt Team
            = (ws + wy) sum_core Lane + ws sum_other Stat + wt Team
with ws, wy, wt = 0.50, 0.25, 0.25 by default (Settings). Stat_total + Style_total + wt Team = Overall.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations

from draftassist.engine.style import StyleModel, StyleRule
from draftassist.engine.synergy import SynergyTable
from draftassist.engine.team import Identity, TeamModel, TeamRule, TeamVs
from draftassist.engine.traits import ChampionTraits

ROLES = ("top", "jungle", "mid", "adc", "support")
CORE_PAIRS: tuple[tuple[str, str], ...] = (("top", "jungle"), ("jungle", "mid"), ("adc", "support"))
CONF_K = 200
SHOW_TEAM_FROM = 3  # roles locked before Team is shown in the plan panel


@dataclass(frozen=True)
class SynergyWeights:
    stat: float = 0.50
    style: float = 0.25
    team: float = 0.25
    conf_k: float = CONF_K

    def __post_init__(self):
        if min(self.stat, self.style, self.team) < 0 or self.stat + self.style <= 0 or self.conf_k <= 0:
            raise ValueError("synergy weights must be >= 0 (stat + style > 0) and conf_k > 0")


def confidence(games: float, k: float = CONF_K) -> float:
    return games / (games + k) if games > 0 else 0.0


def core_term(stat: float, style: float, c: float, w: SynergyWeights = SynergyWeights()) -> float:
    """A core pair's contribution to Overall."""
    return w.stat * c * stat + (w.style + w.stat * (1 - c)) * style


def lane_score(stat: float, style: float, c: float, w: SynergyWeights = SynergyWeights()) -> float:
    return core_term(stat, style, c, w) / (w.stat + w.style)


def is_core(role_a: str, role_b: str) -> bool:
    return (role_a, role_b) in CORE_PAIRS or (role_b, role_a) in CORE_PAIRS


def _ordered(role_a: str, a: int, role_b: str, b: int) -> tuple[str, int, str, int]:
    """Pairs are reported in lane order (top, jungle, mid, adc, support)."""
    if ROLES.index(role_a) <= ROLES.index(role_b):
        return role_a, a, role_b, b
    return role_b, b, role_a, a


@dataclass(frozen=True)
class PairResult:
    role_a: str
    a: int
    role_b: str
    b: int
    stat: float
    style: float
    games: int
    low_data: bool
    confidence: float
    core: bool
    lane: float | None  # core pairs only
    contribution: float  # this pair's part of Overall
    stat_part: float  # ...split into its Stat and Style parts
    style_part: float
    rules: Sequence[tuple[StyleRule, float]] = ()  # top 3 style rules


@dataclass(frozen=True)
class CompResult:
    comp: Mapping[str, int]
    pairs: Sequence[PairResult]
    stat_total: float
    style_total: float
    team: float
    team_weighted: float
    overall: float
    show_team: bool
    team_vs: TeamVs | None
    identity: Identity
    damage: Mapping[str, float]
    bars: Mapping[str, float]
    team_rules: Sequence[tuple[TeamRule, float]] = field(default_factory=tuple)  # top contributions to Team


@dataclass(frozen=True)
class Suggestion:
    role: str
    champ: int
    gain: float  # Overall(comp + champ) - Overall(comp)
    result: CompResult


class SynergyEvaluator:
    def __init__(self, stat: SynergyTable, traits: ChampionTraits, style: StyleModel, team: TeamModel,
                 weights: SynergyWeights | None = None):
        self.stat, self.traits, self.style, self.team = stat, traits, style, team
        self.weights = weights or SynergyWeights()
        self._style: dict[tuple[str, int, str, int], tuple[float, tuple]] = {}

    # ---------- precompute ----------
    def warm(self, champs_by_role: Mapping[str, Iterable[int]]) -> None:
        """Precompute Style for every core pair of these champions and the team rows (after each reload,
        i.e. after every aggregation), so evaluating a comp only adds stored numbers."""
        cols = {r: list(cs) for r, cs in champs_by_role.items()}
        for ra, rb in CORE_PAIRS:
            for a in cols.get(ra, ()):
                for b in cols.get(rb, ()):
                    self._style_of(ra, a, rb, b)
        self.team.features([[(c, r) for r, cs in cols.items() for c in cs[:1]]], fill=True)  # role averages
        for r, cs in cols.items():
            for c in cs:
                self.team.features([[(c, r)]])

    def _style_of(self, ra: str, a: int, rb: str, b: int) -> tuple[float, tuple]:
        key = (ra, a, rb, b)
        if key not in self._style:
            champs = {ra: a, rb: b}
            self._style[key] = (self.style.style(self.traits, champs), tuple(self.style.explain(self.traits, champs)))
        return self._style[key]

    # ---------- pairs ----------
    def pair(self, role_a: str, a: int, role_b: str, b: int) -> PairResult:
        ra, a, rb, b = _ordered(role_a, a, role_b, b)
        ps = self.stat.pair(ra, a, rb, b)
        w = self.weights
        if is_core(ra, rb):
            style, rules = self._style_of(ra, a, rb, b)
            c = confidence(ps.games, w.conf_k)
            stat_part, style_part = w.stat * c * ps.s, (w.style + w.stat * (1 - c)) * style
            return PairResult(ra, a, rb, b, ps.s, style, ps.games, ps.low_data, c, True,
                              lane_score(ps.s, style, c, w), stat_part + style_part, stat_part, style_part, rules)
        stat_part = w.stat * ps.s
        return PairResult(ra, a, rb, b, ps.s, 0.0, ps.games, ps.low_data, confidence(ps.games, w.conf_k), False,
                          None, stat_part, stat_part, 0.0)

    # ---------- comps ----------
    @staticmethod
    def _slots(comp: Mapping[str, int]) -> list[tuple[int, str]]:
        return [(comp[r], r) for r in ROLES if comp.get(r)]

    def evaluate(self, comp: Mapping[str, int], enemy: Sequence[tuple[int, str]] = (), full: bool = True
                 ) -> CompResult:
        """`full=False` skips what Overall doesn't need (identity, damage, bars, Team vs enemy): suggestions."""
        comp = {r: c for r, c in comp.items() if c}
        slots = self._slots(comp)
        pairs = [self.pair(ra, a, rb, b) for (a, ra), (b, rb) in combinations(slots, 2)]
        team = self.team.team(slots, fill=True) if slots else 0.0
        w = self.weights
        stat_total = sum(p.stat_part for p in pairs)
        style_total = sum(p.style_part for p in pairs)
        overall = stat_total + style_total + w.team * team
        if not full:
            return CompResult(comp, pairs, stat_total, style_total, team, w.team * team, overall,
                              len(slots) >= SHOW_TEAM_FROM, None, Identity(None, 0.0, {}), {}, {})
        team_vs = self.team.team_vs(slots, list(enemy), fill=True) if slots and enemy else None
        return CompResult(
            comp, pairs, stat_total, style_total, team, w.team * team, overall,
            len(slots) >= SHOW_TEAM_FROM, team_vs, self.team.identity(slots), self.team.damage_profile(slots),
            self.team.trait_bars(slots), tuple(self.team.contributions(slots, fill=True)[:3]) if slots else ())

    def overall(self, comp: Mapping[str, int]) -> float:
        return self.evaluate(comp).overall

    def suggest(self, comp: Mapping[str, int], pools: Mapping[str, Iterable[int]], unavailable: Iterable[int] = (),
                enemy: Sequence[tuple[int, str]] = (), k: int = 5, roles: Iterable[str] | None = None
                ) -> dict[str, list[Suggestion]]:
        """For each role missing from `comp` (or `roles`): the top k pool champions by Overall gain. Banned,
        picked (either team) and already-used champions are never suggested."""
        comp = {r: c for r, c in comp.items() if c}
        blocked = set(unavailable) | set(comp.values()) | {c for c, _ in enemy}
        base = self.evaluate(comp, enemy, full=False).overall
        out: dict[str, list[Suggestion]] = {}
        for role in (roles or [r for r in ROLES if r not in comp]):
            rows = []
            for c in dict.fromkeys(pools.get(role, ())):
                if c in blocked:
                    continue
                res = self.evaluate({**comp, role: c}, enemy, full=False)
                rows.append(Suggestion(role, c, res.overall - base, res))
            rows.sort(key=lambda s: (-s.gain, s.champ))
            out[role] = rows[:k]
        return out
