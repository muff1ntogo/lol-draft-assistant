"""Matchups smoothed by archetype and kit (docs/matchup-archetypes.md, Stage 3). Pure, no I/O.

For A vs B in a role:

    prior_logodds = logit(role_wr_A) - logit(role_wr_B) + E[arch(A), arch(B)] + sum K[k] * f_k(A, B)
    matchup_wr    = (wins + k_r * sigmoid(prior_logodds)) / (games + k_r)

E has one parameter per unordered pair of archetypes (`E:a>b`: favours a), so it is antisymmetric with E[a, a] = 0.
A kit interaction `K:g*h` reads "A's g against B's h": f(A, B) = g(A) h(B) - g(B) h(A), antisymmetric too.
Archetype weights: primary 1, secondary 0.3; unreviewed labels blend halfway towards the role's average, as tags do.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from draftassist.engine.traits import KitTraits

SECONDARY_WEIGHT = 0.3
UNREVIEWED_WEIGHT = 0.5
K_MIN, K_MAX = 50.0, 400.0
DEFAULT_K = 200.0  # the old PAIR_K, when there's no data to estimate it from
PRIOR_SD_PP = 1.5
PP_PER_LOGODDS = 25.0

# Who A's kit answers: "our g against their h". An `arch:` right side is membership of that archetype.
_INTERACTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("max_hp_damage", ("arch:tank",)),  # shreds tanks
    ("auto_denial", ("auto_reliant",)),  # Shen W, Malphite E
    ("hard_gap_close", ("kite",)),  # reaches kiters
    ("anti_dive", ("arch:assassin", "arch:diver")),  # punishes whoever jumps on it
    ("sustain", ("ability_range",)),  # out-heals poke
)


def interactions_for(words: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """The interactions a role can have: an archetype side must exist in that role's vocabulary."""
    out = []
    for left, rights in _INTERACTIONS:
        for right in rights:
            if not right.startswith("arch:") or right[5:] in words:
                out.append((left, right))
    return tuple(out)


def param_names(words: Sequence[str], interactions: Sequence[tuple[str, str]]) -> list[str]:
    pairs = [f"E:{words[i]}>{words[j]}" for i in range(len(words)) for j in range(i + 1, len(words))]
    return pairs + [f"K:{g}*{h}" for g, h in interactions]


def smooth(wins: float, games: float, k: float, prior_wr: float) -> float:
    return (wins + k * prior_wr) / (games + k)


class RoleFeatures:
    """Labels of a role's champions as arrays, indexed by position in `champs`."""

    def __init__(self, role: str, words: Sequence[str], champs: Sequence[int], labels: Mapping[int, object],
                 kit: KitTraits, interactions: Sequence[tuple[str, str]] | None = None):
        self.role, self.words, self.champs = role, tuple(words), list(champs)
        self.index = {c: i for i, c in enumerate(self.champs)}
        self.interactions = tuple(interactions) if interactions is not None else interactions_for(words)
        self.names = param_names(self.words, self.interactions)
        self.weights = self._archetype_weights(labels)
        n, q = len(self.champs), len(self.interactions)
        self.left, self.right = np.zeros((n, q)), np.zeros((n, q))
        for k, (g, h) in enumerate(self.interactions):
            self.left[:, k] = self._column(g, kit)
            self.right[:, k] = self._column(h, kit)
        self._i, self._j = np.triu_indices(len(self.words), 1)

    def _archetype_weights(self, labels: Mapping[int, object]) -> np.ndarray:
        own = np.zeros((len(self.champs), len(self.words)))
        at = {w: i for i, w in enumerate(self.words)}
        for row, c in enumerate(self.champs):
            label = labels.get(c)
            if label is None:
                continue
            own[row, at[label.primary]] = 1.0
            if label.secondary:
                own[row, at[label.secondary]] = SECONDARY_WEIGHT
        avg = own.mean(axis=0) if len(self.champs) else own.sum(axis=0)
        out = own.copy()
        for row, c in enumerate(self.champs):
            label = labels.get(c)
            if label is None or not label.reviewed:
                out[row] = UNREVIEWED_WEIGHT * own[row] + (1 - UNREVIEWED_WEIGHT) * avg
        return out

    def _column(self, term: str, kit: KitTraits) -> np.ndarray:
        if term.startswith("arch:"):
            return self.weights[:, self.words.index(term[5:])]
        values = [kit.intensity(c, self.role, term) for c in self.champs]
        # no kit tags: typical for the role, so it doesn't read as "has none"
        return np.array([kit.role_average(self.role, term) if v is None else v for v in values], dtype=float)

    def pair_features(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """One row per pair, one column per parameter in `names`."""
        wa, wb = self.weights[a], self.weights[b]
        e = wa[:, self._i] * wb[:, self._j] - wa[:, self._j] * wb[:, self._i]
        k = self.left[a] * self.right[b] - self.left[b] * self.right[a]
        return np.hstack([e, k])

    def matrix(self, coefs: np.ndarray) -> np.ndarray:
        """Predicted log-odds of row vs column from the labels alone (no strength). Antisymmetric."""
        n_e = len(self._i)
        w = self.weights
        cross = w[:, self._i] * coefs[:n_e] @ w[:, self._j].T
        out = cross - cross.T
        kit = (self.left * coefs[n_e:]) @ self.right.T
        return out + kit - kit.T


def fit_role(feats: RoleFeatures, a: np.ndarray, b: np.ndarray, games: np.ndarray, wins: np.ndarray,
             strength: np.ndarray, prior: np.ndarray, prior_sd_pp: float = PRIOR_SD_PP) -> np.ndarray:
    """Ridge on each pair's residual: logit(wr) minus the strength difference, weighted by its precision (games / 4),
    centred on the written priors. Few games leave the priors; many games take over. Coefficients in log-odds."""
    p = len(prior)
    penalty = (PP_PER_LOGODDS / prior_sd_pp) ** 2
    if len(a) == 0:
        return np.asarray(prior, dtype=float).copy()
    wr = (wins + 0.5) / (games + 1.0)
    y = np.log(wr / (1 - wr)) - (strength[a] - strength[b])
    x = feats.pair_features(a, b)
    w = games / 4.0
    xtw = x.T * w
    return np.linalg.solve(xtw @ x + penalty * np.eye(p), xtw @ y + penalty * prior)


def empirical_k(games: np.ndarray, wins: np.ndarray, predicted: np.ndarray) -> float:
    """How much champion data to trust, by the method of moments: the spread of matchups around the prediction beyond
    binomial noise. A pair's true win rate ~ (p, pq / (k + 1)), so k = pq / tau2 - 1. Clamped to [50, 400]."""
    if len(games) == 0 or games.sum() <= 0:
        return DEFAULT_K
    seen = games > 0
    n, pq = games[seen], predicted[seen] * (1 - predicted[seen])
    residual = wins[seen] / n - predicted[seen]
    tau2 = float(np.sum(n * (residual ** 2 - pq / n)) / n.sum())
    if tau2 <= 0:
        return K_MAX
    return float(min(max(float(np.average(pq, weights=n)) / tau2 - 1, K_MIN), K_MAX))
