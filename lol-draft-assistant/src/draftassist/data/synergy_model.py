"""Synergy v2 fits (docs/synergy-v2.md, Stage 2): crawled data + rule files -> synergy_fits -> SynergyModel.

- Style: each core pair's Stat residual (synergy_pairs, bracket "all") regressed on its rules' strengths
  (engine/style.py).
- Team: logistic fit on every crawled match in the window, blue vs red (engine/team.py); the average comp's
  features are stored per bracket (and pooled) for Team = f(comp) - f(average comp).

Run by aggregation (after the traits are rebuilt) and after a prior is edited on the Rules page. Fitting
pools every bracket: interaction effects need the sample size, as for the Stat score.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import Engine, text
from sqlmodel import Session, delete, select

from draftassist.data.rules import STYLE_RULES_FILE, TEAM_RULES_FILE, load_style_rules, load_team_rules
from draftassist.data.synergy import ALL, crawl_weights, latest_window, load_synergy
from draftassist.data.traits import load_traits
from draftassist.db.models import SynergyFit
from draftassist.engine.style import PairObs, StyleModel, fit_style, pair_of_roles
from draftassist.engine.team import TeamModel, fit_team
from draftassist.engine.traits import ChampionTraits

log = logging.getLogger(__name__)


def _settings_sd(db: Engine) -> float:
    from draftassist.data.settings_store import get_synergy_v2_settings

    return get_synergy_v2_settings(db)["prior_sd_pp"]


def _pair_observations(db: Engine, window: str) -> list[PairObs]:
    table = load_synergy(db, window, ALL)
    out = []
    for ((ra, ca), (rb, cb)), ps in table.pairs.items():
        pair = pair_of_roles(ra, rb)
        if pair is not None and ps.games > 0:
            out.append(PairObs(pair, {ra: ca, rb: cb}, ps.s_raw, ps.games))
    return out


def _matches(db: Engine, patches: Mapping[str, float]):
    """(bracket, blue comp, red comp, blue won, weight) for every clean 5v5 match in the window."""
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(patches)))
    params = {}
    for i, (p, w) in enumerate(patches.items()):
        params |= {f"p{i}": p, f"w{i}": w}
    with db.connect() as conn:
        rows = conn.execute(text(
            f"WITH w(patch, weight) AS (VALUES {values}) "
            "SELECT mp.match_id, cm.bracket, w.weight, mp.team_id, mp.champ_id, mp.role, mp.win "
            "FROM match_participants mp JOIN crawled_matches cm ON cm.match_id = mp.match_id "
            "JOIN w ON w.patch = cm.patch ORDER BY mp.match_id"), params).all()
    games: dict[str, dict] = {}
    for match_id, bracket, weight, team, champ, role, win in rows:
        g = games.setdefault(match_id, {"bracket": bracket, "weight": weight, 100: [], 200: [], "won": None})
        g[team].append((champ, role))
        if team == 100:
            g["won"] = bool(win)
    return [(g["bracket"], g[100], g[200], g["won"], g["weight"]) for g in games.values()
            if len(g[100]) == 5 and len(g[200]) == 5]


def fit_synergy_model(db: Engine, window: str | None = None, style_path: Path = STYLE_RULES_FILE,
                      team_path: Path = TEAM_RULES_FILE, patches: Mapping[str, float] | None = None) -> dict:
    """Refit every rule for `window` (default: the latest) and store the fits. Returns a summary."""
    window = window or latest_window(db)
    if window is None:
        return {"window": None, "pairs": 0, "matches": 0, "style_rules": 0, "team_rules": 0}
    sd = _settings_sd(db)
    style_rules, team_rules = load_style_rules(style_path), load_team_rules(team_path)
    traits = load_traits(db, window)
    obs = _pair_observations(db, window)
    style_fits = fit_style(style_rules, obs, traits, prior_sd_pp=sd)

    model = TeamModel(team_rules, traits)
    if patches is None:
        with db.connect() as conn:
            patches = crawl_weights(conn, window)
    games = _matches(db, patches)
    blue = [g[1] for g in games]
    red = [g[2] for g in games]
    won = np.array([g[3] for g in games], dtype=bool)
    weights = np.array([g[4] for g in games], dtype=float)
    team_fit = fit_team(model, blue, red, won, weights if len(games) else None, prior_sd_pp=sd)

    rows: list[SynergyFit] = []
    for r in style_rules:
        f = style_fits[r.id]
        rows.append(SynergyFit(window=window, bracket=ALL, model="style", rule_id=r.id, prior_pp=r.prior_pp,
                               value=f.fitted, support=f.support_games))
    for r in team_rules:
        rows.append(SynergyFit(window=window, bracket=ALL, model="team", rule_id=r.id, prior_pp=r.prior_pp,
                               value=team_fit.coefs[r.id], support=team_fit.support.get(r.id, 0)))
    rows.append(SynergyFit(window=window, bracket=ALL, model="team_side", rule_id="intercept",
                           value=team_fit.intercept, support=team_fit.matches))
    if games and model.own_rules:
        feats = model.features(blue + red)
        brackets = np.array([g[0] for g in games] * 2)
        for bracket in [ALL, *sorted(set(brackets))]:
            sel = feats if bracket == ALL else feats[brackets == bracket]
            for r, m in zip(model.own_rules, sel.mean(axis=0)):
                rows.append(SynergyFit(window=window, bracket=bracket, model="team_mean", rule_id=r.id,
                                       value=float(m), support=len(sel)))
    with Session(db) as s:
        s.exec(delete(SynergyFit).where(SynergyFit.window == window))
        s.add_all(rows)
        s.commit()
    return {"window": window, "pairs": len(obs), "matches": len(games), "style_rules": len(style_rules),
            "team_rules": len(team_rules)}


@dataclass(frozen=True)
class FitInfo:
    prior_pp: float | None
    value: float
    support: int


@dataclass
class SynergyModel:
    traits: ChampionTraits
    style: StyleModel
    team: TeamModel
    window: str | None
    bracket: str
    fits: dict[tuple[str, str], FitInfo] = field(default_factory=dict)  # (model, rule id) -> fit
    matches: int = 0


def load_synergy_model(db: Engine, window: str | None = None, bracket: str = ALL,
                       style_path: Path = STYLE_RULES_FILE, team_path: Path = TEAM_RULES_FILE) -> SynergyModel:
    """Rules from the files, coefficients from the stored fits (a rule without a fit uses its prior),
    the average comp of `bracket` (else every bracket pooled)."""
    window = window or latest_window(db)
    traits = load_traits(db, window, bracket)
    try:
        style_rules, team_rules = load_style_rules(style_path), load_team_rules(team_path)
    except (OSError, ValueError) as e:
        log.warning("synergy rule files unreadable: %s", e)
        style_rules, team_rules = [], []
    with Session(db) as s:
        rows = s.exec(select(SynergyFit).where(SynergyFit.window == window)).all() if window else []
    fits = {(r.model, r.rule_id): FitInfo(r.prior_pp, r.value, r.support) for r in rows if r.bracket == ALL}
    means = {r.rule_id: r.value for r in rows if r.model == "team_mean" and r.bracket == bracket}
    if not means:
        means = {r.rule_id: r.value for r in rows if r.model == "team_mean" and r.bracket == ALL}

    def coef(model: str, rule_id: str, prior_pp: float) -> float:
        f = fits.get((model, rule_id))
        return f.value if f is not None and f.prior_pp == prior_pp else prior_pp / 25  # stale fit: the prior

    style = StyleModel(style_rules, {r.id: coef("style", r.id, r.prior_pp) for r in style_rules})
    team = TeamModel(team_rules, traits)
    side = fits.get(("team_side", "intercept"))
    team = team.with_coefs({r.id: coef("team", r.id, r.prior_pp) for r in team_rules},
                           side.value if side else 0.0)
    team = team.with_means(np.array([means.get(r.id, 0.0) for r in team.own_rules]))
    return SynergyModel(traits, style, team, window, bracket, fits, side.support if side else 0)


def build_evaluator(db: Engine, stat_table, bracket: str = ALL, style_path: Path = STYLE_RULES_FILE,
                    team_path: Path = TEAM_RULES_FILE, pools: Mapping | None = None):
    """The Stage 3 evaluator: Stat from the engine's synergy table, Style and Team from the stored fits, weights
    from Settings; warmed for our pools."""
    from draftassist.data.settings_store import synergy_weights
    from draftassist.engine.synergy_v2 import SynergyEvaluator

    model = load_synergy_model(db, bracket=bracket, style_path=style_path, team_path=team_path)
    ev = SynergyEvaluator(stat_table, model.traits, model.style, model.team, synergy_weights(db))
    if pools:
        ev.warm(pools)
    return ev
