"""What each stat was expected to be, and what a game state at 15 minutes is worth (docs/draft-vs-execution.md, Stage 2).
Pure, no I/O: data/execution_model.py feeds it crawled matches.

Lane stats have the shape of the lane matchup (engine/matchup_model.py): strength + archetype + kit, then the pair's own
games. Everything else is a shrunk mean. Every expectation has a residual SD from games it wasn't fitted on.

The state model reads what happened against what was expected, so at expectation P15 is P0 and a lead the draft already
promised isn't counted twice:
    logit(P15) = logit(P0) + sum b_i (actual_i - expected_i)
It has no intercept for the same reason.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from draftassist.engine.matchup_model import (
    RoleFeatures,
    _logistic,
    empirical_k_gold,
    fit_lane_gold,
    lane_strength,
    smooth,
)
from draftassist.engine.mathx import logit, sigmoid
from draftassist.engine.synergy import K as PAIR_K
from draftassist.engine.synergy import PairScore, pair_score

STATE_FEATURES = ("gold", "towers", "plates", "dragons", "grubs", "herald", "timing_gold")
STATE_PRIOR_SD = 10.0  # log-odds: there's plenty of data, this only keeps an empty column at 0
K_SD = 50  # games before a cell's own residual spread counts as much as the pool's
K_MEAN = 100  # games before a champion's own mean counts as much as the role's
K_BRACKET = 200  # games before a bracket's own offset counts in full
K_PAIR_DEATHS = 50  # lanes before a pair's own death rate counts as much as the two champions' rates
SD_FLOOR = 1e-3  # a stat that never varied still has a z-score


@dataclass(frozen=True)
class Expect:
    mean: float
    sd: float


def shrunk_sd(n: float, cell_var: float, pool_var: float, k: float = K_SD) -> float:
    """A cell's residual SD: its own spread with many games, the pool's with few."""
    return float(np.sqrt((n * cell_var + k * pool_var) / (n + k)))


class SdTable:
    """Residual spread per key, and overall. Residuals are from games the expectation wasn't fitted on, so around 0."""

    def __init__(self, pool_var: float, cells: Mapping[str, Sequence[float]], k: float = K_SD):
        self.pool_var, self.cells, self.k = float(pool_var), dict(cells), k

    @classmethod
    def fit(cls, keys: Iterable, values: Iterable[float], k: float = K_SD) -> SdTable:
        by_key: dict[str, list[float]] = {}
        total = count = 0.0
        for key, v in zip(keys, values):
            by_key.setdefault(str(key), []).append(v * v)
            total += v * v
            count += 1
        cells = {key: [len(vs), sum(vs) / len(vs)] for key, vs in by_key.items()}
        return cls(total / count if count else 0.0, cells, k)

    @property
    def pool(self) -> float:
        return max(float(np.sqrt(self.pool_var)), SD_FLOOR)

    def sd(self, key=None) -> float:
        n, var = self.cells.get(str(key), (0, 0.0))
        return max(shrunk_sd(n, var, self.pool_var, self.k), SD_FLOOR)

    def to_dict(self) -> dict:
        return {"pool_var": self.pool_var, "cells": self.cells, "k": self.k}

    @classmethod
    def from_dict(cls, d: Mapping) -> SdTable:
        return cls(d["pool_var"], d["cells"], d["k"])


class LaneTarget:
    """One lane stat in one role, in the shape of the lane matchup. `expected(a, b)` is a's lead over b."""

    def __init__(self, feats: RoleFeatures, coefs: np.ndarray, strength: np.ndarray, k: float,
                 pairs: Mapping[tuple[int, int], tuple[float, float]]):
        self.feats, self.coefs, self.strength, self.k, self.pairs = feats, np.asarray(coefs, dtype=float), np.asarray(strength, dtype=float), k, pairs
        self._labels = feats.matrix(self.coefs) if len(feats.champs) else np.zeros((0, 0))

    @classmethod
    def fit(cls, feats: RoleFeatures, rows: Sequence[tuple[int, int, float]], prior: np.ndarray, prior_sd: float) -> LaneTarget:
        """`rows`: (champion, enemy, the champion's lead)."""
        totals: dict[tuple[int, int], list[float]] = {}
        leads = []
        for a, b, y in rows:
            key, value = ((a, b), y) if a < b else ((b, a), -y)
            cell = totals.setdefault(key, [0.0, 0.0])
            cell[0] += 1
            cell[1] += value
            leads.append(y)
        sigma2 = float(np.var(leads)) if len(leads) > 1 else 1.0
        sigma2 = max(sigma2, 1e-6)
        pairs = {k: tuple(v) for k, v in totals.items()}
        strength = lane_strength([(None, a, b, n, s) for (a, b), (n, s) in pairs.items()], feats.champs)
        ia = np.array([feats.index[a] for a, _ in pairs], dtype=int)
        ib = np.array([feats.index[b] for _, b in pairs], dtype=int)
        n = np.array([v[0] for v in pairs.values()], dtype=float)
        mean = np.array([v[1] for v in pairs.values()], dtype=float) / n if len(n) else n
        coefs = fit_lane_gold(feats, ia, ib, n, mean, strength, prior, sigma2, prior_sd)
        predicted = strength[ia] - strength[ib] + (feats.pair_features(ia, ib) @ coefs if len(ia) else 0.0)
        k = empirical_k_gold(n, mean, predicted if len(ia) else n, sigma2)
        return cls(feats, coefs, strength, k, pairs)

    def expected(self, a: int, b: int) -> float:
        index = self.feats.index
        if a == b or a not in index or b not in index:
            return 0.0
        if a > b:
            return -self.expected(b, a)
        i, j = index[a], index[b]
        n, total = self.pairs.get((a, b), (0.0, 0.0))
        return float(smooth(total, n, self.k, float(self.strength[i] - self.strength[j] + self._labels[i, j])))

    def to_dict(self) -> dict:
        return {"champs": self.feats.champs, "coefs": self.coefs.tolist(), "strength": self.strength.tolist(), "k": self.k,
                "pairs": [[a, b, n, s] for (a, b), (n, s) in self.pairs.items()]}

    @classmethod
    def from_dict(cls, d: Mapping, feats: RoleFeatures) -> LaneTarget:
        return cls(feats, d["coefs"], d["strength"], d["k"], {(a, b): (n, s) for a, b, n, s in d["pairs"]})


class ChampMeans:
    """A stat that belongs to one champion: its shrunk mean, plus how far each bracket sits from the rest."""

    def __init__(self, overall: float, means: Mapping[int, float], offsets: Mapping[str, float]):
        self.overall, self.means, self.offsets = overall, means, offsets

    @classmethod
    def fit(cls, keys: Sequence[int], values: Sequence[float], brackets: Sequence[str], k: float = K_MEAN) -> ChampMeans:
        y = np.asarray(values, dtype=float)
        overall = float(y.mean()) if len(y) else 0.0
        sums: dict[int, list[float]] = {}
        for key, v in zip(keys, y):
            cell = sums.setdefault(key, [0.0, 0.0])
            cell[0] += 1
            cell[1] += v
        means = {key: (s + k * overall) / (n + k) for key, (n, s) in sums.items()}
        # The bracket's offset is what's left after who was played there
        by_bracket: dict[str, list[float]] = {}
        for key, v, b in zip(keys, y, brackets):
            cell = by_bracket.setdefault(b, [0.0, 0.0])
            cell[0] += 1
            cell[1] += v - means[key]
        offsets = {b: s / (n + K_BRACKET) for b, (n, s) in by_bracket.items()}
        return cls(overall, means, offsets)

    def mean(self, key: int, bracket: str | None = None) -> float:
        return float(self.means.get(key, self.overall) + self.offsets.get(bracket, 0.0))

    def to_dict(self) -> dict:
        return {"overall": self.overall, "means": {str(k): v for k, v in self.means.items()}, "offsets": self.offsets}

    @classmethod
    def from_dict(cls, d: Mapping) -> ChampMeans:
        return cls(d["overall"], {int(k): v for k, v in d["means"].items()}, d["offsets"])


class PoissonMatchups:
    """A count of deaths: the role's rate x how often this victim dies x how often this opponent kills, then the pair's own."""

    def __init__(self, overall: float, victims: Mapping[int, float], killers: Mapping[int, float],
                 pairs: Mapping[tuple[int, int], tuple[float, float]], factors: Mapping[str, float] | None = None):
        self.overall, self.victims, self.killers, self.pairs, self.factors = overall, victims, killers, pairs, factors or {}

    @classmethod
    def fit(cls, rows: Sequence[tuple[int, int, float]], brackets: Sequence[str] | None = None, k: float = K_MEAN) -> PoissonMatchups:
        overall = sum(r[2] for r in rows) / len(rows) if rows else 0.0
        v_cells: dict[int, list[float]] = {}
        k_cells: dict[int, list[float]] = {}
        p_cells: dict[tuple[int, int], list[float]] = {}
        for victim, killer, y in rows:
            for cells, key in ((v_cells, victim), (k_cells, killer), (p_cells, (victim, killer))):
                cell = cells.setdefault(key, [0.0, 0.0])
                cell[0] += 1
                cell[1] += y

        def factor(cells):
            return {key: ((s + k * overall) / (n + k)) / overall if overall else 1.0 for key, (n, s) in cells.items()}

        factors: dict[str, float] = {}
        if brackets is not None and overall:
            by: dict[str, list[float]] = {}
            for (_, _, y), b in zip(rows, brackets):
                cell = by.setdefault(b, [0.0, 0.0])
                cell[0] += 1
                cell[1] += y
            factors = {b: ((s + K_BRACKET * overall) / (n + K_BRACKET)) / overall for b, (n, s) in by.items()}
        return cls(overall, factor(v_cells), factor(k_cells), {key: tuple(v) for key, v in p_cells.items()}, factors)

    def mean(self, victim: int, killer: int, bracket: str | None = None) -> float:
        prior = self.overall * self.victims.get(victim, 1.0) * self.killers.get(killer, 1.0)
        n, total = self.pairs.get((victim, killer), (0.0, 0.0))
        return float((total + K_PAIR_DEATHS * prior) / (n + K_PAIR_DEATHS) * self.factors.get(bracket, 1.0))

    def to_dict(self) -> dict:
        return {"overall": self.overall, "victims": {str(k): v for k, v in self.victims.items()},
                "killers": {str(k): v for k, v in self.killers.items()},
                "pairs": [[a, b, n, s] for (a, b), (n, s) in self.pairs.items()], "factors": self.factors}

    @classmethod
    def from_dict(cls, d: Mapping) -> PoissonMatchups:
        return cls(d["overall"], {int(k): v for k, v in d["victims"].items()}, {int(k): v for k, v in d["killers"].items()},
                   {(a, b): (n, s) for a, b, n, s in d["pairs"]}, d["factors"])


def fit_ridge(x: np.ndarray, y: np.ndarray, penalty: float) -> np.ndarray:
    return np.linalg.solve(x.T @ x + penalty * np.eye(x.shape[1]), x.T @ y)


# ---------- the state model ----------
def early_of_leader(gold: float, early_blue: float, early_red: float) -> float:
    """How early the team with the lead is. A lead is worth more to an early comp than a late one."""
    return early_blue if gold > 0 else early_red if gold < 0 else 0.0


def state_row(gold: float, towers: float, plates: float, dragons: float, grubs: float, herald: float,
              early_blue: float, early_red: float) -> np.ndarray:
    """Blue's view, each against expectation. `gold` in thousands. Swapping the teams flips every value."""
    return np.array([gold, towers, plates, dragons, grubs, herald, gold * early_of_leader(gold, early_blue, early_red)])


@dataclass(frozen=True)
class StateModel:
    coefs: np.ndarray = field(default_factory=lambda: np.zeros(len(STATE_FEATURES)))

    def logit(self, offset: float, row: np.ndarray) -> float:
        return float(offset + row @ self.coefs)

    def p15(self, offset: float, row: np.ndarray) -> float:
        return sigmoid(self.logit(offset, row))


def fit_state(rows: np.ndarray, offset: np.ndarray, won: np.ndarray) -> StateModel:
    """`offset`: the calibrated draft log-odds, taken as given."""
    return StateModel(_logistic(rows, np.asarray(won, dtype=float), np.asarray(offset, dtype=float),
                                np.zeros(rows.shape[1]), STATE_PRIOR_SD))


def fit_draft_scale(draft: np.ndarray, won: np.ndarray) -> float:
    """What the draft model's log-odds are worth in real games: the multiplier that calibrates them."""
    x = np.asarray(draft, dtype=float)[:, None]
    return float(_logistic(x, np.asarray(won, dtype=float), np.zeros(len(x)), np.ones(1), STATE_PRIOR_SD)[0])


def pair_stat_leak(score: PairScore, won: bool, weight: float, games: float, p_a: float, p_b: float) -> float:
    """How much of a pair's Stat came from this one game: Stat with it minus Stat without it, from what the score keeps."""
    if games <= 0:
        return 0.0
    e = logit(p_a) + logit(p_b)
    p_obs = sigmoid(score.s_raw + e)
    wins = p_obs * (games + PAIR_K) - PAIR_K * sigmoid(e)
    return score.s - pair_score(wins - (weight if won else 0.0), games - weight, p_a, p_b).s


# ---------- calibration ----------
def brier(p: np.ndarray, won: np.ndarray) -> float:
    return float(np.mean((np.asarray(p, dtype=float) - np.asarray(won, dtype=float)) ** 2))


def reliability_table(p: np.ndarray, won: np.ndarray) -> list[dict]:
    """Ten 10-point bins: how often the predicted chance came true."""
    p, won = np.asarray(p, dtype=float), np.asarray(won, dtype=float)
    rows = []
    for i in range(10):
        low, high = i / 10, (i + 1) / 10
        at = (p >= low) & (p < high if i < 9 else p <= high)
        n = int(at.sum())
        rows.append({"bin": f"{i * 10}-{(i + 1) * 10}%", "n": n, "predicted": float(p[at].mean()) if n else None,
                     "win_rate": float(won[at].mean()) if n else None})
    return rows
