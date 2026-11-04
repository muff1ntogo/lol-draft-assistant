"""Bot lane is 2v2 (docs/matchup-archetypes.md, Stage 5). Pure, no I/O.

On top of ADC v ADC and support v support, two tables over (ADC archetype, support archetype):
- cross: an ADC of that archetype against an enemy support of that archetype;
- pair: an ADC of that archetype with a support of that archetype on its own team.
Each in log-odds of winning (game) and in gold at 15 minutes (lane). Both are fitted on what's left after each champion's
own strength, so a kind of support that simply wins more can't look good with everyone.

P(win) adds only the cross terms: the ADC-support pairing is already in synergy (Stat and Style).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from draftassist.engine.mathx import logit, sigmoid
from draftassist.engine.matchup_model import LANE_PRIOR_SD_GOLD, PP_PER_LOGODDS, PRIOR_SD_PP, _logistic


PARTNER_TIER_WEIGHT = {"main": 3.0, "comfortable": 2.0, "learning": 1.0}  # who we expect in a role that isn't locked


def bot_features(adc_blue: np.ndarray, sup_blue: np.ndarray, adc_red: np.ndarray, sup_red: np.ndarray) -> np.ndarray:
    """One row per match, blue's view. Columns: the cross table, then the pair table, each (ADC archetype, support one)."""
    def outer(a, s):
        return (a[:, :, None] * s[:, None, :]).reshape(len(a), -1)

    return np.hstack([outer(adc_blue, sup_red) - outer(adc_red, sup_blue),
                      outer(adc_blue, sup_blue) - outer(adc_red, sup_red)])


def fit_bot_game(x: np.ndarray, offset: np.ndarray, won: np.ndarray) -> np.ndarray:
    """Log-odds. `offset`: both lanes' strength and matchups, left-one-out."""
    p = x.shape[1]
    if len(x) == 0:
        return np.zeros(p)
    return _logistic(x, won.astype(float), offset, np.zeros(p), PRIOR_SD_PP / PP_PER_LOGODDS)


def fit_bot_gold(x: np.ndarray, offset: np.ndarray, gold: np.ndarray, sigma2: float) -> np.ndarray:
    """Gold at 15 minutes, both bot players together. `offset`: what strength and labels already predict."""
    p = x.shape[1]
    if len(x) == 0:
        return np.zeros(p)
    return np.linalg.solve(x.T @ x / sigma2 + np.eye(p) / LANE_PRIOR_SD_GOLD ** 2, x.T @ (gold - offset) / sigma2)


@dataclass(frozen=True)
class BotLane:
    adc: Mapping[int, np.ndarray]  # champ -> weights over ADC archetypes
    sup: Mapping[int, np.ndarray]  # champ -> weights over support archetypes
    cross_game: np.ndarray  # (ADC archetype, support archetype) -> log-odds for the ADC's side
    pair_game: np.ndarray
    cross_gold: np.ndarray  # gold for the ADC's side
    pair_gold: np.ndarray
    labels: Mapping[tuple[int, str], str] | None = None  # (champ, role) -> main archetype, for reasons
    slope: float = 0.4  # log-odds per 1,000 gold

    def _term(self, table: np.ndarray, adc: int | None, sup: int | None) -> float:
        wa, ws = self.adc.get(adc), self.sup.get(sup)
        return 0.0 if wa is None or ws is None else float(wa @ table @ ws)

    def cross(self, adc: int | None, enemy_sup: int | None) -> float:
        return self._term(self.cross_game, adc, enemy_sup)

    def pair(self, adc: int | None, sup: int | None) -> float:
        return self._term(self.pair_game, adc, sup)

    def game_cross(self, our_adc: int | None, our_sup: int | None, their_adc: int | None, their_sup: int | None) -> float:
        """What P(win) adds: each ADC against the other side's support."""
        return self.cross(our_adc, their_sup) - self.cross(their_adc, our_sup)

    def resp_adjust(self, our_adc: int | None, our_sup: int | None, their_adc: int | None, their_sup: int | None,
                    lane_weight: float) -> float:
        """Log-odds added to a 1v1 for a lane ban Resp: cross and pair, lane and game weighted as the 1v1s are."""
        def side(cross, pair) -> float:
            return (self._term(cross, our_adc, their_sup) - self._term(cross, their_adc, our_sup)
                    + self._term(pair, our_adc, our_sup))

        game = side(self.cross_game, self.pair_game)
        gold = side(self.cross_gold, self.pair_gold)
        return lane_weight * gold / 1000 * self.slope + (1 - lane_weight) * game

    def pair_label(self, champ: int, role: str) -> str | None:
        return (self.labels or {}).get((champ, role))


class BotLaneData:
    """A lane ban data provider with the other two bot laners in view: each ADC or support answer is scored with our
    partner (locked, else the pool weighted by tier) and the enemy's, where known."""

    def __init__(self, base, bot: BotLane, partners: Mapping[str, list[tuple[int, float]]],
                 enemy: Mapping[str, int | None], lane_weight: float):
        self._base, self._bot, self._partners, self._enemy, self._w = base, bot, partners, enemy, lane_weight

    def __getattr__(self, name):
        return getattr(self._base, name)

    def matchup_wr(self, role: str, ours: int, theirs: int) -> float | None:
        wr = self._base.matchup_wr(role, ours, theirs)
        if wr is None or role not in ("adc", "support"):
            return wr
        total = 0.0
        for partner, share in self._partners.get("support" if role == "adc" else "adc", ()):
            if role == "adc":
                total += share * self._bot.resp_adjust(ours, partner, theirs, self._enemy.get("support"), self._w)
            else:
                total += share * self._bot.resp_adjust(partner, ours, self._enemy.get("adc"), theirs, self._w)
        return sigmoid(logit(wr) + total)
