"""Lane ban score: how much banning X improves our player's expected matchup quality.

Per role, with w(X) the opponents' pick rates and Resp(X) our player's best answer to X in
percentage points:

    mu          = sum w(X) * Resp(X) / W
    BanScore(X) = w(X) / (W - w(X)) * (mu - Resp(X))       (exactly mu' - mu after removing X)

TeamLaneBan(X) sums BanScore over our five roles. Pure functions; data comes in through the
LaneBanData protocol so tests and the engine can supply it without a database.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Protocol

from draftassist.engine.model import (
    ROLES,
    BracketStats,
    EngineData,
    LaneBanConfig,
    OurTeam,
)

__all__ = ["LaneBanConfig"]  # re-exported; defined in model to avoid an import cycle

NO_MATCHUP_DATA = "(no matchup data)"
NO_ELIGIBLE_POOL = "(no eligible pool champion)"
_DEGENERATE_EPS = 1e-6


class LaneBanData(Protocol):
    def opponents(self, role: str) -> Iterable[int]:
        """Champions seen in the enemy's version of `role` (patch, enemy bracket)."""

    def pick_rate(self, champ: int, role: str) -> float: ...

    def ban_rate(self, champ: int) -> float: ...

    def matchup_wr(self, role: str, ours: int, theirs: int) -> float | None:
        """Shrunk lane win rate of `ours` vs `theirs` (our player's bracket); None if no data."""

    def role_wr(self, champ: int, role: str) -> float | None:
        """Shrunk average win rate of `champ` in `role` (our player's bracket)."""


@dataclass(frozen=True)
class BanScore:
    ban: float
    degenerate: bool = False  # X was effectively the only opponent; score forced to 0


@dataclass(frozen=True)
class LaneBanRow:
    opp: int
    pick_rate: float  # raw pick rate, for display
    w: float  # weight used (ban-adjusted if configured)
    resp: float  # pp
    via: int | None  # pool champion delivering Resp
    via_note: str | None  # NO_MATCHUP_DATA / NO_ELIGIBLE_POOL when via is None
    ban: float
    degenerate: bool = False


@dataclass(frozen=True)
class RoleBanTable:
    role: str
    mu: float
    rows: tuple[LaneBanRow, ...]  # sorted by ban, descending
    enemy_filled: bool = False

    def row(self, champ: int) -> LaneBanRow | None:
        return next((r for r in self.rows if r.opp == champ), None)


# ---------- core formula ----------
def expected_quality(w: Mapping[int, float], resp: Mapping[int, float]) -> float:
    total = sum(w.values())
    return sum(w[x] * resp[x] for x in w) / total if total > 0 else 0.0


def ban_scores(w: Mapping[int, float], resp: Mapping[int, float]) -> dict[int, BanScore]:
    total = sum(w.values())
    mu = expected_quality(w, resp)
    out = {}
    for x, wx in w.items():
        rest = total - wx
        if rest < _DEGENERATE_EPS:
            out[x] = BanScore(0.0, degenerate=True)
        else:
            out[x] = BanScore(wx / rest * (mu - resp[x]))
    return out


# ---------- per role ----------
def _resp(data: LaneBanData, role: str, opp: int, answers: list[int], tiers: Mapping[int, str],
          cfg: LaneBanConfig) -> tuple[float, int | None, str | None]:
    if not answers:
        return 0.0, None, NO_ELIGIBLE_POOL
    best: tuple[float, int] | None = None
    for p in answers:
        if p == opp:  # no mirror matchups: if they have X, we can't play X
            continue
        wr = data.matchup_wr(role, p, opp)
        if wr is None:
            continue
        baseline = 0.5
        if cfg.delta_baseline == "champion_avg":
            baseline = data.role_wr(p, role) or 0.5
        value = 100 * (wr - baseline) + cfg.tier_adj.get(tiers.get(p, ""), 0.0)
        if best is None or value > best[0]:
            best = (value, p)
    if best is not None:
        return best[0], best[1], None
    # No matchup data: the player's baseline with their best champion in the role. The same for
    # every such opponent (X itself not excluded), so missing data never creates a ban preference.
    role_wrs = [wr for p in answers if (wr := data.role_wr(p, role)) is not None]
    if not role_wrs:
        return 0.0, None, NO_MATCHUP_DATA
    top = max(role_wrs)
    baseline = top if cfg.delta_baseline == "champion_avg" else 0.5
    return 100 * (top - baseline), None, NO_MATCHUP_DATA


def role_ban_table(
    data: LaneBanData,
    role: str,
    pool: Mapping[int, str],  # our player's pool for this role: champ -> tier
    unavailable: AbstractSet[int] = frozenset(),
    locked: int | None = None,
    enemy_filled: bool = False,
    config: LaneBanConfig | None = None,
) -> RoleBanTable:
    cfg = config or LaneBanConfig()
    pick_rates, w = {}, {}
    for x in sorted(set(data.opponents(role))):
        if x in unavailable:
            continue
        pr = data.pick_rate(x, role)
        if pr <= 0:
            continue
        weight = pr
        if cfg.adjust_for_bans:
            available_share = 1 - data.ban_rate(x)
            weight = pr / available_share if available_share > _DEGENERATE_EPS else pr
        pick_rates[x], w[x] = pr, weight

    answers = [locked] if locked is not None else sorted(p for p in pool if p not in unavailable)
    resp, via, notes = {}, {}, {}
    for x in w:
        resp[x], via[x], notes[x] = _resp(data, role, x, answers, pool, cfg)

    scores = ban_scores(w, resp)
    rows = [
        LaneBanRow(
            opp=x, pick_rate=pick_rates[x], w=w[x], resp=resp[x], via=via[x], via_note=notes[x],
            ban=0.0 if enemy_filled else scores[x].ban,
            degenerate=scores[x].degenerate,
        )
        for x in w
    ]
    rows.sort(key=lambda r: (-r.ban, r.opp))
    return RoleBanTable(role, expected_quality(w, resp), tuple(rows), enemy_filled)


def team_lane_ban(tables: Iterable[RoleBanTable], role_weights: Mapping[str, float] | None = None) -> dict[int, float]:
    """`role_weights`: how much each role's matchups count (docs/matchup-archetypes.md, Stage 4)."""
    total: dict[int, float] = {}
    for table in tables:
        weight = role_weights.get(table.role, 1.0) if role_weights else 1.0
        for row in table.rows:
            total[row.opp] = total.get(row.opp, 0.0) + weight * row.ban
    return total


# ---------- data providers ----------
@dataclass(frozen=True)
class MappingLaneData:
    """Dict-backed provider for tests and fixtures."""
    pick_rates: Mapping[str, Mapping[int, float]]  # role -> champ -> pick rate
    ban_rates: Mapping[int, float] = field(default_factory=dict)
    matchups: Mapping[tuple[str, int, int], float] = field(default_factory=dict)  # (role, ours, theirs)
    role_wrs: Mapping[tuple[str, int], float] = field(default_factory=dict)  # (role, champ)

    def opponents(self, role: str) -> Iterable[int]:
        return self.pick_rates.get(role, {}).keys()

    def pick_rate(self, champ: int, role: str) -> float:
        return self.pick_rates.get(role, {}).get(champ, 0.0)

    def ban_rate(self, champ: int) -> float:
        return self.ban_rates.get(champ, 0.0)

    def matchup_wr(self, role: str, ours: int, theirs: int) -> float | None:
        if (wr := self.matchups.get((role, ours, theirs))) is not None:
            return wr
        if (wr := self.matchups.get((role, theirs, ours))) is not None:
            return 1 - wr
        return None

    def role_wr(self, champ: int, role: str) -> float | None:
        return self.role_wrs.get((role, champ))


class BracketLaneData:
    """Provider over the engine's stats: opponents and their pick/ban rates from the enemy
    bracket; matchups and our champions' averages from our player's bracket for the role."""

    def __init__(self, data: EngineData, team: OurTeam, enemy_bracket: str, lane_weight: float = 0.0):
        self._lane_weight = lane_weight
        self._enemy: BracketStats = data.brackets[enemy_bracket]
        self._ours = {r: data.brackets[b] for r, b in team.brackets.items()}
        self._opponents = {r: sorted(c for c, roles in self._enemy.meta.items()
                                     if r in roles and roles[r].pick_rate > 0) for r in ROLES}

    def opponents(self, role: str) -> Iterable[int]:
        return self._opponents[role]

    def pick_rate(self, champ: int, role: str) -> float:
        stat = self._enemy.role_stat(champ, role)
        return stat.pick_rate if stat else 0.0

    def ban_rate(self, champ: int) -> float:
        return self._enemy.ban_rates.get(champ, 0.0)

    def matchup_wr(self, role: str, ours: int, theirs: int) -> float | None:
        return self._ours[role].blended_wr(role, ours, theirs, self._lane_weight)

    def role_wr(self, champ: int, role: str) -> float | None:
        stat = self._ours[role].role_stat(champ, role)
        return stat.win_rate if stat else None
