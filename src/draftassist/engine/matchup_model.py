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

from draftassist.engine.traits import UNREVIEWED_WEIGHT, KitTraits

SECONDARY_WEIGHT = 0.3
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


# The lane matchup: the mean gold lead at 15 minutes, in the same shape as the game matchup but linear.
GOLD_PER_PP = 100.0  # a written prior of 0.5pp = 50 gold
LANE_PRIOR_SD_GOLD = 150.0


def fit_lane_gold(feats: RoleFeatures, a: np.ndarray, b: np.ndarray, games: np.ndarray, mean: np.ndarray,
                  strength: np.ndarray, prior: np.ndarray, sigma2: float,
                  prior_sd: float = LANE_PRIOR_SD_GOLD) -> np.ndarray:
    """Ridge on each pair's mean lead minus the difference of the two champions' own mean leads, weighted by
    games / sigma2 (sigma2: one lane's variance). Coefficients in gold."""
    if len(a) == 0:
        return np.asarray(prior, dtype=float).copy()
    x = feats.pair_features(a, b)
    xtw = x.T * (games / sigma2)
    penalty = 1.0 / prior_sd ** 2
    return np.linalg.solve(xtw @ x + penalty * np.eye(len(prior)), xtw @ (mean - (strength[a] - strength[b])) + penalty * prior)


def empirical_k_gold(games: np.ndarray, mean: np.ndarray, predicted: np.ndarray, sigma2: float) -> float:
    """As empirical_k: k = noise variance of one lane / true variance of pairs around the prediction."""
    if len(games) == 0 or games.sum() <= 0:
        return DEFAULT_K
    seen = games > 0
    n = games[seen]
    tau2 = float(np.sum(n * ((mean[seen] - predicted[seen]) ** 2 - sigma2 / n)) / n.sum())
    if tau2 <= 0:
        return K_MAX
    return float(min(max(sigma2 / tau2, K_MIN), K_MAX))


LANE_STRENGTH_K = 100  # a champion's own mean lead shrinks towards 0 by this many lanes


def lane_strength(pairs: Sequence, champs: Sequence[int]) -> np.ndarray:
    """Each champion's mean lead against everyone, shrunk towards 0. `pairs`: (anything, a, b, lanes, a's summed lead)."""
    lanes = dict.fromkeys(champs, 0.0)
    gold = dict.fromkeys(champs, 0.0)
    for _, a, b, n, g in pairs:
        lanes[a], gold[a] = lanes[a] + n, gold[a] + g
        lanes[b], gold[b] = lanes[b] + n, gold[b] - g
    return np.array([gold[c] / (lanes[c] + LANE_STRENGTH_K) for c in champs])


DEFAULT_SLOPE = 1.0  # log-odds per 1,000 gold
SLOPE_PRIOR_SD = 0.5  # log-odds, around the role's own slope
ROLE_WEIGHT_PRIOR_SD = 0.5


def _logistic(x: np.ndarray, won: np.ndarray, offset: np.ndarray, prior: np.ndarray, sd: float) -> np.ndarray:
    """MAP logistic regression with a Gaussian prior, by Newton's method. The coefficients."""
    beta = prior.astype(float).copy()
    penalty = 1.0 / sd ** 2
    for _ in range(25):
        eta = offset + x @ beta
        p = 1 / (1 + np.exp(-eta))
        grad = x.T @ (won - p) - penalty * (beta - prior)
        hess = (x.T * (p * (1 - p))) @ x + penalty * np.eye(len(beta))
        step = np.linalg.solve(hess, grad)
        beta += step
        if np.abs(step).max() < 1e-6:
            break
    return beta


def fit_lane_slopes(arch: np.ndarray, lead: np.ndarray, offset: np.ndarray, won: np.ndarray, n_arch: int) -> np.ndarray:
    """What a lane lead is worth, per archetype: logit(win) = offset + slope[arch] * lead, with the lead from
    the champion's own side, in thousands of gold. Pulled towards the role's overall slope. This is an association within the same game, not
    pure cause: being ahead also marks the better player and the game being won."""
    if len(arch) == 0:
        return np.full(n_arch, DEFAULT_SLOPE)
    decided = lead != 0
    won_f = won.astype(float)
    overall = _logistic(lead[decided, None], won_f[decided], offset[decided], np.array([DEFAULT_SLOPE]), 10.0)[0]
    x = np.zeros((int(decided.sum()), n_arch))
    x[np.arange(len(x)), arch[decided]] = lead[decided]
    return _logistic(x, won_f[decided], offset[decided], np.full(n_arch, overall), SLOPE_PRIOR_SD)


def fit_role_weights(edges: np.ndarray, offset: np.ndarray, won: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """How much each role's game matchup predicts wins: the coefficients of logit(blue wins) = offset + sum w_r * edge_r,
    from 1 with a Gaussian prior, floored at 0, normalised so the roles average 1. Returns (weights, standard errors)."""
    q = edges.shape[1]
    if len(edges) == 0:
        return np.ones(q), np.full(q, ROLE_WEIGHT_PRIOR_SD)
    beta = _logistic(edges, won.astype(float), offset, np.ones(q), ROLE_WEIGHT_PRIOR_SD)
    p = 1 / (1 + np.exp(-(offset + edges @ beta)))
    hess = (edges.T * (p * (1 - p))) @ edges + np.eye(q) / ROLE_WEIGHT_PRIOR_SD ** 2
    se = np.sqrt(np.diag(np.linalg.inv(hess)))
    beta = np.maximum(beta, 0.0)
    scale = beta.mean()
    return (beta / scale, se / scale) if scale > 0 else (np.ones(q), se)
