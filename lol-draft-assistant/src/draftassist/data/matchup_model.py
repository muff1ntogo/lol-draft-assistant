"""Matchup model fits (docs/matchup-archetypes.md, Stage 3): crawled matchups + labels + written priors -> matchup_fits.

Run by every aggregation. E, K and k_r pool every crawled patch and bracket (the same decayed window as synergy):
interaction effects need the sample size and are stable across patches. A pair's own wins and games, and the strength
in its prior, come from the current patch and the player's bracket (data/provider.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import Engine, text
from sqlmodel import Session, delete, select

from draftassist.data.archetypes import (
    ARCHETYPES,
    PRIORS_FILE,
    load_archetype_priors,
    priors_for,
    resolve_archetype,
)
from draftassist.data.synergy import crawl_weights, latest_window, weights_cte
from draftassist.data.tags import load_archetypes, load_kit
from draftassist.db.models import Champion, MatchupFit
from draftassist.engine.matchup_model import (
    DEFAULT_K,
    PP_PER_LOGODDS,
    PRIOR_SD_PP,
    RoleFeatures,
    empirical_k,
    fit_role,
    interactions_for,
    param_names,
)
from draftassist.engine.model import ROLES, RoleStat
from draftassist.engine.traits import KitTraits

K_PARAM = "k_r"
# Strength in the prior shrinks much harder than the engine's role win rates (k = 50). Held-out lanes on 16.21, 18.8k
# matches, 5 folds, skill = (coin - log-loss) x 1000 summed over roles: k=50 -2.75, 200 +0.55, 500 +1.86, 1000 +2.15,
# 2000 +1.89. A champion's real strength varies by about 2pp, so a few hundred games are mostly noise.
STRENGTH_K = 1000
PAIR_PICK_FLOOR = 0.001  # estimates for champions picked this often in a role; rarer ones only when they've met


def _labels(db: Engine) -> tuple[dict, dict, KitTraits]:
    with Session(db) as s:
        classes = {c.champ_id: c.tags for c in s.exec(select(Champion)).all()}
    return load_archetypes(db), classes, KitTraits(load_kit(db))


def _features(role: str, champs: list[int], written: Mapping, classes: Mapping, kit: KitTraits) -> RoleFeatures:
    labels = {c: resolve_archetype(written, c, role, classes.get(c)) for c in champs}
    return RoleFeatures(role, ARCHETYPES[role], champs, labels, kit)


def fit_matchup_model(db: Engine, window: str | None = None, priors_path: Path = PRIORS_FILE,
                      prior_sd_pp: float = PRIOR_SD_PP) -> dict:
    """Fit every role on the game matchups of `window` (default: the latest) and store it. Returns k_r per role."""
    window = window or latest_window(db)
    if window is None:
        return {}
    priors = load_archetype_priors(priors_path)
    written, classes, kit = _labels(db)
    with db.connect() as conn:
        values, params = weights_cte(crawl_weights(conn, window))
        pairs = conn.execute(text(
            f"WITH w(patch, weight) AS (VALUES {values}) "
            "SELECT role, champ_a, champ_b, SUM(games * weight), SUM(wins_a * weight) "
            "FROM matchups JOIN w USING (patch) GROUP BY role, champ_a, champ_b"), params).all()
        roles = conn.execute(text(
            f"WITH w(patch, weight) AS (VALUES {values}) "
            "SELECT role, champ_id, SUM(games * weight), SUM(wins * weight) "
            "FROM champ_role_stats JOIN w USING (patch) GROUP BY role, champ_id"), params).all()
    wr = {(r, c): (wins + 0.5 * STRENGTH_K) / (games + STRENGTH_K) for r, c, games, wins in roles}
    rows, out = [], {}
    for role in ROLES:
        here = [p for p in pairs if p[0] == role]
        champs = sorted({c for p in here for c in p[1:3]} | {c for r, c in wr if r == role})
        feats = _features(role, champs, written, classes, kit)
        wanted = priors_for(priors, role, "game")
        prior = np.array([wanted.get(tuple(n[2:].split(">")), 0.0) / PP_PER_LOGODDS if n.startswith("E:") else 0.0
                          for n in feats.names])
        a = np.array([feats.index[p[1]] for p in here], dtype=int)
        b = np.array([feats.index[p[2]] for p in here], dtype=int)
        n = np.array([p[3] for p in here], dtype=float)
        wins = np.array([p[4] for p in here], dtype=float)
        p_champ = np.array([wr.get((role, c), 0.5) for c in champs])
        strength = np.log(p_champ / (1 - p_champ)) if len(champs) else p_champ
        coefs = fit_role(feats, a, b, n, wins, strength, prior, prior_sd_pp)
        logodds = strength[a] - strength[b] + (feats.pair_features(a, b) @ coefs if len(a) else 0.0)
        k = empirical_k(n, wins, 1 / (1 + np.exp(-logodds)) if len(a) else n)
        support = round(float(n.sum()))
        rows += [MatchupFit(window=window, role=role, param=name, value=float(v),
                            prior_pp=float(pv) * PP_PER_LOGODDS, support=support)
                 for name, v, pv in zip(feats.names, coefs, prior)]
        rows.append(MatchupFit(window=window, role=role, param=K_PARAM, value=k, support=support))
        out[role] = k
    with Session(db) as s:
        s.exec(delete(MatchupFit).where(MatchupFit.window == window))
        s.add_all(rows)
        s.commit()
    return out


@dataclass
class MatchupModel:
    window: str
    _coefs: Mapping[str, Mapping[str, float]]
    _k: Mapping[str, float]
    written: Mapping
    classes: Mapping
    kit: KitTraits
    _matrices: dict = field(default_factory=dict)

    def coefs(self, role: str) -> Mapping[str, float]:
        return self._coefs.get(role, {})

    def k(self, role: str) -> float:
        return self._k.get(role, DEFAULT_K)

    def features(self, role: str) -> RoleFeatures:
        return _features(role, sorted(self.classes), self.written, self.classes, self.kit)

    def prediction(self, role: str) -> tuple[Mapping[int, int], np.ndarray]:
        """(champ -> index, label-only log-odds of row vs column) for every champion, built once per role."""
        if role not in self._matrices:
            feats = self.features(role)
            coefs = np.array([self.coefs(role).get(n, 0.0) for n in feats.names])
            self._matrices[role] = (feats.index, feats.matrix(coefs))
        return self._matrices[role]


def load_matchup_model(db: Engine, window: str | None = None) -> MatchupModel | None:
    """The stored fit of `window` (default: the latest), or None before the first fit."""
    window = window or latest_window(db)
    if window is None:
        return None
    with Session(db) as s:
        rows = s.exec(select(MatchupFit).where(MatchupFit.window == window)).all()
    if not rows:
        return None
    coefs: dict[str, dict[str, float]] = {}
    ks: dict[str, float] = {}
    for r in rows:
        if r.param == K_PARAM:
            ks[r.role] = r.value
        else:
            coefs.setdefault(r.role, {})[r.param] = r.value
    written, classes, kit = _labels(db)
    return MatchupModel(window, coefs, ks, written, classes, kit)


def smoothed_matchups(model: MatchupModel, meta: Mapping[int, Mapping[str, RoleStat]], observed: list,
                      strength: Mapping[tuple[int, str], float]
                      ) -> tuple[dict[tuple[str, int, int], float], dict[tuple[str, int, int], int]]:
    """One bracket's matchups: every pair of its real picks in a role, plus every pair that has met. `observed` holds
    its Matchup rows on the current patch; `strength` is each champion's log-odds in the prior. Returns (a's win rate,
    games), keys (role, a, b) with a < b."""
    met: dict[str, dict[tuple[int, int], tuple[int, int]]] = {}
    for m in observed:
        met.setdefault(m.role, {})[(m.champ_a, m.champ_b)] = (m.wins_a, m.games)
    matchups, games = {}, {}
    for role in ROLES:
        index, labels_only = model.prediction(role)
        k = model.k(role)
        pairs = met.get(role, {})
        picked = {c for c, roles in meta.items() if role in roles and roles[role].pick_rate >= PAIR_PICK_FLOOR}
        champs = sorted((picked | {c for p in pairs for c in p}) & set(index))
        if len(champs) < 2:
            continue
        at = np.array([index[c] for c in champs])
        s = np.array([strength.get((c, role), 0.0) for c in champs])
        logodds = s[:, None] - s[None, :] + labels_only[np.ix_(at, at)]
        prior = 1 / (1 + np.exp(-logodds))
        for i, a in enumerate(champs):
            for j in range(i + 1, len(champs)):
                b = champs[j]
                wins, n = pairs.get((a, b), (0, 0))
                matchups[(role, a, b)] = (wins + k * float(prior[i, j])) / (n + k)
                if n:
                    games[(role, a, b)] = n
    return matchups, games


def format_matchup_report(db: Engine, window: str | None = None) -> str:
    """E, K and k_r per role. E is shown as the effect on typical champions of each archetype (labels as they stand:
    unreviewed ones blend towards the role, so a raw coefficient overstates what a real pair gets)."""
    model = load_matchup_model(db, window)
    if model is None:
        return "Matchup model: not fitted yet (run an aggregation)."
    lines = [f"Matchup model, window {model.window}: pp of win chance, + = first archetype favoured; [prior]"]
    for role in ROLES:
        fits = _report_fits(db, model.window, role)
        support = fits.get(K_PARAM, (0, None, 0))[2]
        lines.append(f"\n{role.upper()}  k_r {model.k(role):.0f}  ({support:,} weighted games)")
        index, labels_only = model.prediction(role)
        primary = {c: resolve_archetype(model.written, c, role, model.classes.get(c)).primary for c in index}
        for name in param_names(ARCHETYPES[role], interactions_for(ARCHETYPES[role])):
            value, prior, _ = fits.get(name, (0.0, 0.0, 0))
            if name.startswith("E:"):
                a, b = name[2:].split(">")
                ia = [index[c] for c, p in primary.items() if p == a]
                ib = [index[c] for c, p in primary.items() if p == b]
                typical = float(labels_only[np.ix_(ia, ib)].mean()) if ia and ib else 0.0
                lines.append(f"  {a} v {b}: {typical * PP_PER_LOGODDS:+.2f} [{prior or 0:+.2f}]")
            else:
                lines.append(f"  kit {name[2:]}: {value * PP_PER_LOGODDS:+.2f}")
    return "\n".join(lines)


def _report_fits(db: Engine, window: str, role: str) -> dict[str, tuple[float, float | None, int]]:
    with Session(db) as s:
        rows = s.exec(select(MatchupFit).where(MatchupFit.window == window, MatchupFit.role == role)).all()
    return {r.param: (r.value, r.prior_pp, r.support) for r in rows}
