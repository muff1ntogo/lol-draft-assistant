"""Pair style rules for the core in-lane pairs (docs/synergy-v2.md, Stage 2a). Pure, no I/O.

A rule names a core pair and one trait term per role, with a prior effect in pp:
    {pair: ADC-SUP, when: {SUP: enchanter, ADC: hypercarry}, prior_pp: +2.0, note: ...}
Its strength for two champions is the product of their intensities (engine/traits.py; each 0-1). A rule
whose term is unknown for either champion (no tag, no data) does not fire.

Fit: each core pair's Stat residual s_raw is regressed on the strengths of its pair's rules, by ridge
regression centred on the priors (prior mean prior_pp / 25, prior SD 1pp). s_raw is shrunk towards 0 by
n / (n + k) (engine/synergy.py), so the expected s_raw is c * x . beta with c = n / (n + k); the strengths
are scaled by c and each pair is weighted by the precision of its s_raw, about (n + k)^2 / (4 n), i.e. by
its games. With no data the fit returns the priors; with little data it stays close to them.

Style(A, B) = sum over the pair's rules of fitted coefficient * strength.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from draftassist.engine.traits import ChampionTraits, term_value, validate_term

ROLE_CODES = {"TOP": "top", "JG": "jungle", "MID": "mid", "ADC": "adc", "SUP": "support"}
CODE_OF = {v: k for k, v in ROLE_CODES.items()}
PAIRS: dict[str, tuple[str, str]] = {"TOP-JG": ("top", "jungle"), "JG-MID": ("jungle", "mid"),
                                     "ADC-SUP": ("adc", "support")}
PRIOR_SD_PP = 1.0
K = 200  # the Stat shrinkage (engine/synergy.py)
SUPPORT_STRENGTH = 0.25  # a pair "supports" a rule when the rule's strength for it is at least this
FLAG_DRIFT_PP = 1.5  # Rules page: flag a fit this far from its prior, or of the other sign


def pair_of_roles(role_a: str, role_b: str) -> str | None:
    for name, roles in PAIRS.items():
        if {role_a, role_b} == set(roles):
            return name
    return None


@dataclass(frozen=True)
class StyleRule:
    pair: str
    when: tuple[tuple[str, str], ...]  # ((role, term), (role, term)), in the order written
    prior_pp: float
    note: str = ""

    @property
    def id(self) -> str:
        return f"{self.pair}:" + ",".join(f"{CODE_OF[r]}={t}" for r, t in self.when)

    @property
    def roles(self) -> tuple[str, str]:
        return PAIRS[self.pair]

    def strength(self, traits: ChampionTraits, champs: Mapping[str, int]) -> float:
        out = 1.0
        for role, term in self.when:
            v = term_value(traits, champs[role], role, term)
            if v is None:
                return 0.0
            out *= v
        return out

    def as_dict(self) -> dict:
        return {"pair": self.pair, "when": {CODE_OF[r]: t for r, t in self.when}, "prior_pp": self.prior_pp,
                "note": self.note}


def parse_style_rules(items: Sequence[Mapping]) -> list[StyleRule]:
    rules, seen = [], set()
    for i, item in enumerate(items):
        where = f"style rule {i + 1}"
        pair = str(item.get("pair", ""))
        if pair not in PAIRS:
            raise ValueError(f"{where}: pair must be one of {', '.join(PAIRS)}, got {pair!r}")
        when = item.get("when") or {}
        roles = {ROLE_CODES.get(str(k)): v for k, v in when.items()}
        if set(roles) != set(PAIRS[pair]) or len(when) != 2:
            raise ValueError(f"{where}: `when` needs one term for each of {pair}")
        try:
            terms = tuple((r, validate_term(t)) for r, t in roles.items())
            prior = float(item["prior_pp"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{where}: {e}") from None
        rule = StyleRule(pair, terms, prior, str(item.get("note") or ""))
        key = (pair, tuple(sorted(terms)))
        if key in seen:
            raise ValueError(f"{where}: duplicate rule {rule.id}")
        seen.add(key)
        rules.append(rule)
    return rules


@dataclass(frozen=True)
class PairObs:
    """One core pair of champions from the crawl: its Stat residual s_raw and raw games together."""
    pair: str
    champs: Mapping[str, int]  # role -> champ
    s_raw: float
    games: float


@dataclass(frozen=True)
class RuleFit:
    fitted: float  # log-odds at full strength
    support_games: int  # games of the pairs with strength >= SUPPORT_STRENGTH

    @property
    def fitted_pp(self) -> float:
        return 25 * self.fitted


def ridge(X: np.ndarray, y: np.ndarray, w: np.ndarray, mu: np.ndarray, tau: float | np.ndarray) -> np.ndarray:
    """argmin sum w (y - X b)^2 + sum ((b - mu) / tau)^2."""
    lam = np.diag(1 / np.broadcast_to(np.asarray(tau, dtype=float), mu.shape) ** 2)
    a = X.T @ (w[:, None] * X) + lam
    return np.linalg.solve(a, X.T @ (w * y) + lam @ mu)


def fit_style(rules: Sequence[StyleRule], observations: Sequence[PairObs], traits: ChampionTraits,
              prior_sd_pp: float = PRIOR_SD_PP, k: float = K) -> dict[str, RuleFit]:
    """Fitted coefficient per rule id (log-odds), each core pair fitted on its own pairs."""
    out: dict[str, RuleFit] = {}
    for pair in PAIRS:
        group = [r for r in rules if r.pair == pair]
        if not group:
            continue
        mu = np.array([r.prior_pp / 25 for r in group])
        obs = [o for o in observations if o.pair == pair and o.games > 0]
        if not obs:
            out |= {r.id: RuleFit(float(m), 0) for r, m in zip(group, mu)}
            continue
        X = np.array([[r.strength(traits, o.champs) for r in group] for o in obs])
        n = np.array([o.games for o in obs], dtype=float)
        c = n / (n + k)
        w = (n + k) ** 2 / (4 * n)
        beta = ridge(X * c[:, None], np.array([o.s_raw for o in obs]), w, mu, prior_sd_pp / 25)
        support = ((X >= SUPPORT_STRENGTH) * n[:, None]).sum(axis=0)
        out |= {r.id: RuleFit(float(b), int(s)) for r, b, s in zip(group, beta, support)}
    return out


def flagged(prior_pp: float, fitted_pp: float, drift_pp: float = FLAG_DRIFT_PP) -> bool:
    """The data disagrees with the written rule: far from the prior, or the other sign."""
    return abs(fitted_pp - prior_pp) > drift_pp or (prior_pp * fitted_pp < 0)


@dataclass(frozen=True)
class StyleModel:
    rules: Sequence[StyleRule]
    coefs: Mapping[str, float]  # rule id -> log-odds (fitted, else the prior)

    def coef(self, rule: StyleRule) -> float:
        return self.coefs.get(rule.id, rule.prior_pp / 25)

    def contributions(self, traits: ChampionTraits, champs: Mapping[str, int]) -> list[tuple[StyleRule, float]]:
        pair = pair_of_roles(*champs) if len(champs) == 2 else None
        out = []
        for r in self.rules:
            if r.pair != pair:
                continue
            s = r.strength(traits, champs)
            if s:
                out.append((r, self.coef(r) * s))
        return out

    def style(self, traits: ChampionTraits, champs: Mapping[str, int]) -> float:
        """Style(A, B) in log-odds for a core pair given as {role: champ}; 0 for any other pair."""
        return sum(v for _, v in self.contributions(traits, champs))

    def explain(self, traits: ChampionTraits, champs: Mapping[str, int], k: int = 3) -> list[tuple[StyleRule, float]]:
        """The k rules contributing most (by size), with their contribution in log-odds."""
        return sorted(self.contributions(traits, champs), key=lambda rv: -abs(rv[1]))[:k]

    @staticmethod
    def label(item: tuple[StyleRule, float]) -> str:
        rule, v = item
        pp = 25 * v
        return f"{rule.note or rule.id} {'+' if pp >= 0 else '−'}{abs(pp):.1f}"
