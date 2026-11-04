"""Normalised duo synergy (docs/synergy-visualizer.md, A3/A4). Pure, no I/O.

For champion A in role r1 and B in role r2 on the same team:
    e     = logit(p_A) + logit(p_B)                      expected log-odds if they don't interact
    p_obs = (wins_AB + k * sigmoid(e)) / (games_AB + k)   shrunk towards the expectation, k = 200
    s_raw = logit(p_obs) - e                              interaction only; 0 when data is thin
    s     = 0.3 * tanh(s_raw / 0.3)                       soft cap at about +-7.5pp
Shown in percentage points as 25 * s (the slope of the sigmoid at 50% is 1/4).
A trio scores the mean of its three pairs and carries its weakest pair.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from draftassist.engine.mathx import logit, sigmoid

K = 200  # shrinkage games towards the no-interaction expectation
ROLE_K = 50  # champion strength p_A shrinks towards 50% (spec: role win rates)
CAP = 0.3  # log-odds
LOW_DATA_GAMES = 30  # fewer raw games: stored and scored, but shown as low data


def to_pp(s: float) -> float:
    return 25 * s


def strength(wins: float, games: float, k: float = ROLE_K) -> float:
    """Champion win rate in a role, shrunk towards 50%."""
    return (wins + 0.5 * k) / (games + k)


@dataclass(frozen=True)
class PairScore:
    s: float  # capped interaction, log-odds
    s_raw: float  # before the soft cap
    games: int  # raw games together
    low_data: bool

    @property
    def pp(self) -> float:
        return to_pp(self.s)


NO_DATA = PairScore(0.0, 0.0, 0, True)


def pair_score(wins: float, games: float, p_a: float, p_b: float, k: float = K, cap: float = CAP,
               raw_games: int | None = None) -> PairScore:
    """`wins`/`games` may be time-decay weighted; `raw_games` (default: games) drives the low-data flag."""
    if games <= 0:
        return NO_DATA
    e = logit(p_a) + logit(p_b)
    p_obs = (wins + k * sigmoid(e)) / (games + k)
    s_raw = _logit(p_obs) - e
    n = round(games) if raw_games is None else raw_games
    return PairScore(cap * math.tanh(s_raw / cap), s_raw, n, n < LOW_DATA_GAMES)


def _logit(p: float) -> float:
    # nearly unclamped: the usual 1e-4 clamp would bias extreme expectations
    return logit(p, eps=1e-12)


Slot = tuple[str, int]  # (role, champ)


@dataclass(frozen=True)
class SynergyTable:
    """Scored pairs for one window and bracket. Look up in either order; missing pairs score 0."""
    window: str | None
    bracket: str
    pairs: Mapping[tuple[Slot, Slot], PairScore]  # key ordered: first slot < second
    strengths: Mapping[Slot, float]  # (role, champ) -> shrunk win rate

    def pair(self, role_a: str, a: int, role_b: str, b: int) -> PairScore:
        x, y = (role_a, a), (role_b, b)
        return self.pairs.get((x, y) if x < y else (y, x), NO_DATA)

    def strength(self, champ: int, role: str) -> float:
        return self.strengths.get((role, champ), 0.5)


EMPTY = SynergyTable(None, "all", {}, {})
