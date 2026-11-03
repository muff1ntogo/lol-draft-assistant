"""Team model: how well a whole comp fights outside lane (docs/synergy-v2.md, Stage 2b). Pure, no I/O.

Each rule turns the comp's trait intensities (engine/traits.py) into one feature in 0-1:
    {max: engage}                 the best champion's intensity ("has")
    {mean: term}                  average over the champions
    {sum: frontline, cap: 2}      amount, capped and scaled: min(sum, cap) / cap
    {lack: frontline, need: 1}    shortfall: max(0, need - sum) / need
    {excess: hypercarry, over: 1, cap: 1}   surplus: min(max(0, sum - over), cap) / cap
    {protect: hypercarry, by: [peel, enchanter]}   best carry x best protector among the others
    {all: [spec, spec]}           product of features
    {special: damage_imbalance | curve_coherence | identity_coherence}
Cross-team rules multiply our feature by theirs: {ours: {max: engage}, theirs: {max: disengage}}.
Terms may be compound ("hypercarry * not dive"). An unknown intensity (no tag, no data) is filled with the
role's average for that term, so it reads as typical rather than 0.

Fit (logistic regression on crawled matches, blue's view):
    logit P(blue wins) = b0 + (f(blue) - f(red)) . beta + (x(blue, red) - x(red, blue)) . gamma
with ridge priors centred on prior_pp / 25 (SD 1pp); b0 (side) is not penalised.

Team(comp) = (f(comp) - f(average comp in the bracket)) . beta, in log-odds.
Team vs enemy = (f(ours) - f(theirs)) . beta + (x(ours, theirs) - x(theirs, ours)) . gamma.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from draftassist.engine.comp import IDENTITIES, IDENTITY_LABELS, IDENTITY_TERMS
from draftassist.engine.traits import ChampionTraits, term_role_averages, term_value, validate_term

Comp = Sequence[tuple[int, str]]  # (champ, role); any subset of the five roles
ROLE_INDEX = {"top": 0, "jungle": 1, "mid": 2, "adc": 3, "support": 4}
PRIOR_SD_PP = 1.0
AGGS = ("max", "mean", "sum", "lack", "excess", "protect", "all", "special")
SPECIALS = ("damage_imbalance", "curve_coherence", "identity_coherence")
DAMAGE_TERMS = ("dmg_phys", "dmg_magic", "dmg_true")
IMBALANCE_FROM = 0.70  # share of one damage type where the imbalance feature starts (1 at 100%)

# the five comp identities are the shared fight-style vocabulary (engine/comp.py)
BAR_TERMS = ("engage", "frontline_all", "peel_all", "enchanter", "poke", "power_curve")


def _spec_terms(spec: Mapping) -> list[str]:
    if "all" in spec:
        return [t for s in spec["all"] for t in _spec_terms(s)]
    if "special" in spec:
        name = spec["special"]
        if name == "damage_imbalance":
            return list(DAMAGE_TERMS)
        if name == "curve_coherence":
            return ["power_curve"]
        return [t for terms in IDENTITY_TERMS.values() for t, _ in terms]
    if "protect" in spec:
        return [spec["protect"], *spec["by"]]
    agg = next(k for k in spec if k in AGGS)
    return [spec[agg]]


def _validate_spec(spec, where: str) -> dict:
    if not isinstance(spec, Mapping):
        raise ValueError(f"{where}: a feature must be a mapping")
    aggs = [k for k in spec if k in AGGS]
    if len(aggs) != 1:
        raise ValueError(f"{where}: a feature needs exactly one of {', '.join(AGGS)}")
    agg = aggs[0]
    out = dict(spec)
    if agg == "all":
        out["all"] = [_validate_spec(s, where) for s in spec["all"]]
    elif agg == "special":
        if spec["special"] not in SPECIALS:
            raise ValueError(f"{where}: special must be one of {', '.join(SPECIALS)}")
    elif agg == "protect":
        out["protect"] = validate_term(spec["protect"])
        out["by"] = [validate_term(t) for t in spec.get("by") or []]
        if not out["by"]:
            raise ValueError(f"{where}: protect needs `by`")
    else:
        out[agg] = validate_term(spec[agg])
        if agg == "sum":
            out["cap"] = float(spec.get("cap", 1))
        if agg == "lack":
            out["need"] = float(spec.get("need", 1))
        if agg == "excess":
            out["over"] = float(spec.get("over", 1))
            out["cap"] = float(spec.get("cap", 1))
    return out


@dataclass(frozen=True)
class TeamRule:
    id: str
    prior_pp: float
    note: str = ""
    feature: Mapping | None = None  # own-team rule
    ours: Mapping | None = None  # cross-team rule: ours x theirs
    theirs: Mapping | None = None

    @property
    def cross(self) -> bool:
        return self.ours is not None

    def as_dict(self) -> dict:
        d: dict = {"id": self.id}
        if self.cross:
            d |= {"ours": dict(self.ours), "theirs": dict(self.theirs)}
        else:
            d["feature"] = dict(self.feature)
        return d | {"prior_pp": self.prior_pp, "note": self.note}


def parse_team_rules(items: Sequence[Mapping]) -> list[TeamRule]:
    rules, seen = [], set()
    for i, item in enumerate(items):
        rid = str(item.get("id") or "")
        where = f"team rule {rid or i + 1}"
        if not rid or rid in seen:
            raise ValueError(f"{where}: every rule needs a unique id")
        seen.add(rid)
        try:
            prior = float(item["prior_pp"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{where}: prior_pp must be a number") from None
        note = str(item.get("note") or "")
        if "ours" in item or "theirs" in item:
            rules.append(TeamRule(rid, prior, note, ours=_validate_spec(item.get("ours"), where),
                                  theirs=_validate_spec(item.get("theirs"), where)))
        else:
            rules.append(TeamRule(rid, prior, note, feature=_validate_spec(item.get("feature"), where)))
    return rules


@dataclass(frozen=True)
class Identity:
    label: str | None  # engage / poke / pick / protect / split, None for an empty comp
    coherence: float  # share of the champions' strongest identity that points at the label
    scores: Mapping[str, float]

    def as_dict(self) -> dict:
        return {"label": self.label, "name": IDENTITY_LABELS.get(self.label), "coherence": round(self.coherence, 3),
                "scores": {k: round(v, 3) for k, v in self.scores.items()}}


@dataclass(frozen=True)
class TeamVs:
    total: float  # log-odds
    own: float
    cross: float
    contributions: Sequence[tuple[TeamRule, float]]  # sorted by size


@dataclass
class _Arrays:
    I: np.ndarray  # (N, 5, T) intensities
    M: np.ndarray  # (N, 5) slot present


class TeamModel:
    def __init__(self, rules: Sequence[TeamRule], traits: ChampionTraits, coefs: Mapping[str, float] | None = None,
                 means: np.ndarray | None = None, intercept: float = 0.0):
        self.rules = list(rules)
        self.traits = traits
        self.own_rules = [r for r in self.rules if not r.cross]
        self.cross_rules = [r for r in self.rules if r.cross]
        self.coefs = {r.id: r.prior_pp / 25 for r in self.rules} | dict(coefs or {})
        self.means = np.zeros(len(self.own_rules)) if means is None else np.asarray(means, dtype=float)
        self.intercept = intercept
        terms: list[str] = []
        for r in self.rules:
            for spec in ((r.feature,) if not r.cross else (r.ours, r.theirs)):
                terms += _spec_terms(spec)
        terms += [t for ts in IDENTITY_TERMS.values() for t, _ in ts] + list(DAMAGE_TERMS) + list(BAR_TERMS)
        self.terms = list(dict.fromkeys(terms))
        self.col = {t: i for i, t in enumerate(self.terms)}
        self._avg: dict[str, np.ndarray] = {}
        self._rows: dict[tuple[int, str], np.ndarray] = {}

    # ---------- copies ----------
    def with_coefs(self, coefs: Mapping[str, float], intercept: float = 0.0) -> TeamModel:
        m = self._copy()
        m.coefs = {r.id: r.prior_pp / 25 for r in self.rules} | dict(coefs)
        m.intercept = intercept
        return m

    def with_means(self, means: np.ndarray) -> TeamModel:
        m = self._copy()
        m.means = np.asarray(means, dtype=float)
        return m

    def _copy(self) -> TeamModel:
        m = TeamModel.__new__(TeamModel)
        m.__dict__ = dict(self.__dict__)
        return m

    # ---------- intensities ----------
    def _role_avg(self, role: str) -> np.ndarray:
        if role not in self._avg:
            avg = term_role_averages(self.traits, role, self.terms)
            self._avg[role] = np.array([avg[t] for t in self.terms])
        return self._avg[role]

    def _row(self, champ: int, role: str) -> np.ndarray:
        key = (champ, role)
        if key not in self._rows:
            avg = self._role_avg(role)
            self._rows[key] = np.array([
                v if (v := term_value(self.traits, champ, role, t)) is not None else avg[i]
                for i, t in enumerate(self.terms)])
        return self._rows[key]

    def _arrays(self, comps: Sequence[Comp], fill: bool = False) -> _Arrays:
        n, t = len(comps), len(self.terms)
        keys: dict[tuple[int, str], int] = {}
        idx = np.full((n, 5), -1, dtype=np.int64)
        for i, comp in enumerate(comps):
            for champ, role in comp:
                k = (champ, role)
                r = keys.get(k)
                if r is None:
                    r = keys[k] = len(keys)
                idx[i, ROLE_INDEX[role]] = r
        table = np.zeros((len(keys) + 1, t))  # last row: an empty slot
        for k, r in keys.items():
            table[r] = self._row(*k)
        M = idx >= 0
        I = table[np.where(M, idx, len(keys))]
        if fill:
            for role, j in ROLE_INDEX.items():
                missing = ~M[:, j]
                if missing.any():
                    I[missing, j] = self._role_avg(role)
            M = np.ones_like(M)
        return _Arrays(I, M)

    # ---------- features ----------
    def _term(self, a: _Arrays, term: str) -> np.ndarray:
        return a.I[:, :, self.col[term]] * a.M  # (N, 5), absent slots 0

    def _feature(self, a: _Arrays, spec: Mapping) -> np.ndarray:
        if "all" in spec:
            out = np.ones(len(a.M))
            for s in spec["all"]:
                out = out * self._feature(a, s)
            return out
        if "special" in spec:
            return getattr(self, f"_{spec['special']}")(a)
        if "protect" in spec:
            carry = self._term(a, spec["protect"])
            prot = np.max(np.stack([self._term(a, t) for t in spec["by"]]), axis=0)
            # best carry x best protector among the others
            best = np.zeros(len(a.M))
            for j in range(5):
                others = np.delete(prot, j, axis=1).max(axis=1)
                best = np.maximum(best, carry[:, j] * others)
            return best
        if "max" in spec:
            return self._term(a, spec["max"]).max(axis=1)
        if "mean" in spec:
            count = a.M.sum(axis=1)
            return np.where(count > 0, self._term(a, spec["mean"]).sum(axis=1) / np.maximum(count, 1), 0.0)
        if "sum" in spec:
            cap = spec["cap"]
            return np.minimum(self._term(a, spec["sum"]).sum(axis=1), cap) / cap
        if "excess" in spec:
            over = np.maximum(self._term(a, spec["excess"]).sum(axis=1) - spec["over"], 0.0)
            return np.minimum(over, spec["cap"]) / spec["cap"]
        if "lack" in spec:
            need = spec["need"]
            return np.maximum(need - self._term(a, spec["lack"]).sum(axis=1), 0.0) / need
        raise ValueError(f"bad feature {spec}")

    def _shares(self, a: _Arrays) -> np.ndarray:
        d = np.stack([self._term(a, t) for t in DAMAGE_TERMS], axis=2)  # (N, 5, 3)
        tot = d.sum(axis=(1, 2))
        return d.sum(axis=1) / np.where(tot > 0, tot, 1)[:, None]

    def _damage_imbalance(self, a: _Arrays) -> np.ndarray:
        top = self._shares(a).max(axis=1)
        return np.clip((top - IMBALANCE_FROM) / (1 - IMBALANCE_FROM), 0.0, 1.0)

    def _curve_coherence(self, a: _Arrays) -> np.ndarray:
        v = self._term(a, "power_curve")
        count = np.maximum(a.M.sum(axis=1), 1)
        mean = v.sum(axis=1) / count
        sd = np.sqrt((((v - mean[:, None]) ** 2) * a.M).sum(axis=1) / count)
        return np.where(a.M.sum(axis=1) >= 2, 1 - np.minimum(1.0, 2 * sd), 0.0)

    def _identity_parts(self, a: _Arrays) -> np.ndarray:
        """(N, 5, identities): each champion's contribution to each identity."""
        return np.stack([sum(w * self._term(a, t) for t, w in IDENTITY_TERMS[k]) for k in IDENTITIES], axis=2)

    def _identity_coherence(self, a: _Arrays) -> np.ndarray:
        parts = self._identity_parts(a)
        label = parts.sum(axis=1).argmax(axis=1)
        aligned = np.take_along_axis(parts, label[:, None, None], axis=2)[:, :, 0].sum(axis=1)
        best = parts.max(axis=2).sum(axis=1)
        return np.where(best > 0, aligned / np.where(best > 0, best, 1), 0.0)

    def features(self, comps: Sequence[Comp], fill: bool = False) -> np.ndarray:
        """(N, own rules) features in 0-1. `fill`: missing roles become an average champion of the role."""
        a = self._arrays(comps, fill)
        if not self.own_rules:
            return np.zeros((len(comps), 0))
        return np.stack([self._feature(a, r.feature) for r in self.own_rules], axis=1)

    def cross(self, ours: Sequence[Comp], theirs: Sequence[Comp], fill: bool = False) -> np.ndarray:
        """(N, cross rules): our side's feature x their side's feature."""
        a, b = self._arrays(ours, fill), self._arrays(theirs, fill)
        if not self.cross_rules:
            return np.zeros((len(ours), 0))
        return np.stack([self._feature(a, r.ours) * self._feature(b, r.theirs) for r in self.cross_rules], axis=1)

    # ---------- scores ----------
    def beta(self) -> np.ndarray:
        return np.array([self.coefs[r.id] for r in self.own_rules])

    def gamma(self) -> np.ndarray:
        return np.array([self.coefs[r.id] for r in self.cross_rules])

    def team(self, comp: Comp, fill: bool = False) -> float:
        """Team(comp) in log-odds, relative to the average comp."""
        return float((self.features([comp], fill)[0] - self.means) @ self.beta())

    def contributions(self, comp: Comp, fill: bool = False) -> list[tuple[TeamRule, float]]:
        d = (self.features([comp], fill)[0] - self.means) * self.beta()
        return sorted(zip(self.own_rules, map(float, d)), key=lambda rv: -abs(rv[1]))

    def team_vs(self, ours: Comp, theirs: Comp, fill: bool = False) -> TeamVs:
        own = (self.features([ours], fill)[0] - self.features([theirs], fill)[0]) * self.beta()
        cross = (self.cross([ours], [theirs], fill)[0] - self.cross([theirs], [ours], fill)[0]) * self.gamma()
        parts = list(zip(self.own_rules, map(float, own))) + list(zip(self.cross_rules, map(float, cross)))
        return TeamVs(float(own.sum() + cross.sum()), float(own.sum()), float(cross.sum()),
                      sorted(parts, key=lambda rv: -abs(rv[1])))

    def vs_batch(self, ours: Comp, theirs: Sequence[Comp], fill: bool = True) -> tuple[np.ndarray, np.ndarray]:
        """Team vs enemy for one comp of ours against many enemy comps at once: (totals (N,), per-rule
        contributions (N, own rules + cross rules)), in log-odds. Used to score ban candidates."""
        n = len(theirs)
        if n == 0:
            return np.zeros(0), np.zeros((0, len(self.rules)))
        f_ours = self.features([ours], fill)[0]
        own = (f_ours[None, :] - self.features(theirs, fill)) * self.beta()
        many = [ours] * n
        cross = (self.cross(many, theirs, fill) - self.cross(theirs, many, fill)) * self.gamma()
        parts = np.hstack([own, cross])
        return parts.sum(axis=1), parts

    def identity_parts(self, champ: int, role: str) -> dict[str, float]:
        """One champion's contribution to each comp identity."""
        parts = self._identity_parts(self._arrays([[(champ, role)]]))[0, ROLE_INDEX[role]]
        return dict(zip(IDENTITIES, map(float, parts)))

    def damage_profile(self, comp: Comp) -> dict[str, float]:
        s = self._shares(self._arrays([comp]))[0]
        return {"physical": float(s[0]), "magic": float(s[1]), "true": float(s[2])}

    def trait_bars(self, comp: Comp) -> dict[str, float]:
        """0-1 bars for the comp panel: engage (best), frontline (amount of across-role percentiles, 3 = full),
        peel (best peel percentile or enchanter), poke (amount, 2 = full), scaling (mean power curve)."""
        a = self._arrays([comp])
        n = max(int(a.M.sum()), 1)
        return {"engage": float(self._term(a, "engage").max()),
                "frontline": float(min(self._term(a, "frontline_all").sum(), 3) / 3),
                "peel": float(max(self._term(a, "peel_all").max(), self._term(a, "enchanter").max())),
                "poke": float(min(self._term(a, "poke").sum(), 2) / 2),
                "scaling": float(self._term(a, "power_curve").sum() / n)}

    def identity(self, comp: Comp) -> Identity:
        a = self._arrays([comp])
        parts = self._identity_parts(a)[0]  # (5, identities)
        scores = parts.sum(axis=0)
        if not comp or scores.max() <= 0:
            return Identity(None, 0.0, dict(zip(IDENTITIES, map(float, scores))))
        k = int(scores.argmax())
        best = parts.max(axis=1).sum()
        return Identity(IDENTITIES[k], float(parts[:, k].sum() / best), dict(zip(IDENTITIES, map(float, scores))))


def comp_identity(traits: ChampionTraits, comp: Comp) -> Identity:
    return TeamModel([], traits).identity(comp)


@dataclass(frozen=True)
class TeamFit:
    coefs: Mapping[str, float]  # rule id -> log-odds
    intercept: float  # blue side
    matches: int
    support: Mapping[str, int] = field(default_factory=dict)  # rule id -> matches where it applied (>= 0.25)


def logistic_ridge(X: np.ndarray, y: np.ndarray, mu: np.ndarray, tau: np.ndarray, w: np.ndarray | None = None,
                   iters: int = 50) -> np.ndarray:
    """MAP logistic regression with independent normal priors N(mu, tau^2) (tau = inf: no penalty)."""
    beta = np.where(np.isfinite(tau), mu, 0.0).astype(float)
    prec = np.where(np.isfinite(tau), 1 / np.where(np.isfinite(tau), tau, 1) ** 2, 0.0)
    w = np.ones(len(y)) if w is None else w
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ beta)))
        grad = X.T @ (w * (y - p)) - prec * (beta - mu)
        h = X.T @ ((w * p * (1 - p))[:, None] * X) + np.diag(prec)
        step = np.linalg.solve(h, grad)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            break
    return beta


def fit_team(model: TeamModel, blue: Sequence[Comp], red: Sequence[Comp], blue_won: np.ndarray,
             weights: np.ndarray | None = None, prior_sd_pp: float = PRIOR_SD_PP) -> TeamFit:
    rules = model.own_rules + model.cross_rules
    mu = np.array([0.0] + [r.prior_pp / 25 for r in rules])
    tau = np.array([np.inf] + [prior_sd_pp / 25] * len(rules))
    if len(blue) == 0:
        return TeamFit({r.id: float(m) for r, m in zip(rules, mu[1:])}, 0.0, 0)
    fb, fr = model.features(blue), model.features(red)
    cb, cr = model.cross(blue, red), model.cross(red, blue)
    X = np.hstack([np.ones((len(blue), 1)), fb - fr, cb - cr])
    beta = logistic_ridge(X, np.asarray(blue_won, dtype=float), mu, tau, weights)
    applied = np.hstack([(fb >= 0.25) | (fr >= 0.25), (cb >= 0.25) | (cr >= 0.25)])
    return TeamFit({r.id: float(b) for r, b in zip(rules, beta[1:])}, float(beta[0]), len(blue),
                   {r.id: int(s) for r, s in zip(rules, applied.sum(axis=0))})


__all__ = ["IDENTITIES", "Identity", "TeamFit", "TeamModel", "TeamRule", "TeamVs", "comp_identity", "fit_team",
           "parse_team_rules"]
