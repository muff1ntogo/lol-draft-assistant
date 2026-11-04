"""Fitting the matchup model (docs/matchup-archetypes.md, Stages 3-6): crawled matchups + labels + written priors ->
matchup_fits. Run by every aggregation. Reading it back: data/matchup_model.py.

E, K and k_r pool every crawled patch and bracket (the same decayed window as synergy): interaction effects need the sample
size and are stable across patches. So do the lane matchup, the lead slopes, role weights and the bot lane tables.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import numpy as np
from sqlalchemy import Engine, text
from sqlmodel import Session, delete

from draftassist.data.archetypes import ARCHETYPES, PRIORS_FILE, load_archetype_priors, priors_for, resolve_archetype
from draftassist.data.matchup_model import (
    BEHIND_GOLD,
    BEHIND_K,
    BEHIND_PREFIX,
    BOT_ROLE,
    DEFAULT_SIGMA2,
    K_PARAM,
    LANE_K,
    LANE_PREFIX,
    ROLE_WEIGHT,
    ROLE_WEIGHT_SE,
    SLOPE_PREFIX,
    STRENGTH_K,
    _features,
    _labels,
    _lane_pairs,
    _lane_strength,
)
from draftassist.data.synergy import crawl_weights, latest_window, weights_cte
from draftassist.db.models import MatchupFit
from draftassist.engine.bot_lane import bot_features, fit_bot_game, fit_bot_gold
from draftassist.engine.matchup_model import (
    GOLD_PER_PP,
    PP_PER_LOGODDS,
    PRIOR_SD_PP,
    RoleFeatures,
    empirical_k,
    empirical_k_gold,
    fit_lane_gold,
    fit_lane_slopes,
    fit_role,
    fit_role_weights,
)
from draftassist.engine.model import ROLES


def _fit_lane(role: str, pairs: list, lanes: list, feats: RoleFeatures, priors: Mapping, window: str) -> list:
    wanted = priors_for(priors, role, "lane")
    prior = np.array([wanted.get(tuple(n[2:].split(">")), 0.0) * GOLD_PER_PP if n.startswith("E:") else 0.0
                      for n in feats.names])
    golds = np.array([r[5] for r in lanes if r[5] is not None], dtype=float)
    sigma2 = float(golds.var()) if len(golds) > 1 else DEFAULT_SIGMA2
    n = np.array([p[3] for p in pairs], dtype=float)
    a = np.array([feats.index[p[1]] for p in pairs], dtype=int)
    b = np.array([feats.index[p[2]] for p in pairs], dtype=int)
    mean = np.array([p[4] for p in pairs], dtype=float) / n if len(pairs) else n
    strength = _lane_strength(pairs, feats.champs)
    coefs = fit_lane_gold(feats, a, b, n, mean, strength, prior, sigma2)
    predicted = strength[a] - strength[b] + (feats.pair_features(a, b) @ coefs if len(a) else 0.0)
    k = empirical_k_gold(n, mean, predicted if len(a) else n, sigma2)
    support = round(float(n.sum()))
    rows = [MatchupFit(window=window, role=role, param=LANE_PREFIX + name, value=float(v), prior_pp=float(pv) / GOLD_PER_PP,
                       support=support) for name, v, pv in zip(feats.names, coefs, prior)]
    rows.append(MatchupFit(window=window, role=role, param=LANE_K, value=k, support=support))
    strength_matrix = strength[:, None] - strength[None, :] if len(strength) else np.zeros((0, 0))
    return rows, strength_matrix + (feats.matrix(coefs) if len(strength) else 0.0)


def _lanes(conn, values: str, params: dict) -> list:
    """Every lane in the window, blue against red, with the result and blue's gold lead at 15 minutes."""
    return conn.execute(text(
        f"WITH w(patch, weight) AS (VALUES {values}) "
        "SELECT a.match_id, a.role, a.champ_id, b.champ_id, a.win, a.lane_gold_15, w.weight "
        "FROM match_participants a JOIN match_participants b ON b.match_id = a.match_id AND b.role = a.role "
        "AND a.team_id = 100 AND b.team_id = 200 "
        "JOIN crawled_matches cm ON cm.match_id = a.match_id JOIN w ON w.patch = cm.patch"), params).all()


def _fit_slopes(role: str, lanes: list, strength: Mapping, written: Mapping, classes: Mapping, window: str) -> list:
    """Each lane twice, from both champions' sides: each lead is worth its own archetype's slope."""
    words = ARCHETYPES[role]
    at = {w: i for i, w in enumerate(words)}
    rows = [r for r in lanes if r[5] is not None]
    main = {c: resolve_archetype(written, c, role, classes.get(c)).primary for r in rows for c in r[2:4]}
    n = len(rows)
    arch = np.zeros(2 * n, dtype=int)
    lead, offset = np.zeros(2 * n), np.zeros(2 * n)
    won = np.zeros(2 * n, dtype=bool)
    for i, (_, _, x, y, win, gold, _w) in enumerate(rows):
        gap = strength.get(x, 0.0) - strength.get(y, 0.0)
        arch[i], lead[i], offset[i], won[i] = at[main[x]], gold / 1000, gap, bool(win)
        j = n + i
        arch[j], lead[j], offset[j], won[j] = at[main[y]], -gold / 1000, -gap, not win
    slopes = fit_lane_slopes(arch, lead, offset, won, len(words))
    return [MatchupFit(window=window, role=role, param=SLOPE_PREFIX + w, value=float(s), support=int((arch == i).sum()))
            for i, (w, s) in enumerate(zip(words, slopes))]


def _fit_role_weights(role_games: Mapping, window: str) -> list:
    """Leave-one-out edges: a game can't be predicted from a pair record that includes it."""
    grouped = _group_by_match(role_games)
    complete = [m for m, by in grouped.items() if len(by) == len(ROLES)]
    edges = np.array([[grouped[m][r][0] for r in ROLES] for m in complete]).reshape(-1, len(ROLES))
    offset = np.array([sum(grouped[m][r][1] for r in ROLES) for m in complete])
    won = np.array([grouped[m][ROLES[0]][2] for m in complete], dtype=bool)
    weights, se = fit_role_weights(edges, offset, won)
    out = []
    for r, w, e in zip(ROLES, weights, se):
        out.append(MatchupFit(window=window, role=r, param=ROLE_WEIGHT, value=float(w), support=len(complete)))
        out.append(MatchupFit(window=window, role=r, param=ROLE_WEIGHT_SE, value=float(e), support=len(complete)))
    return out


def _fit_bot(role_games: Mapping, inputs: Mapping, window: str) -> list:
    """Cross and pair tables (engine/bot_lane.py), on what's left after each champion's strength and the two 1v1s."""
    adc_rows, adc_feats, adc_pred = inputs["adc"]
    sup_rows, sup_feats, sup_pred = inputs["support"]
    sup_by_match = {r[0]: r for r in sup_rows}
    pairs = [(a, sup_by_match[a[0]]) for a in adc_rows if a[0] in sup_by_match]
    n = len(pairs)
    w_adc, w_sup = adc_feats.weights, sup_feats.weights
    idx = lambda f, rows, at: np.array([f.index[r[at]] for r in rows], dtype=int)
    a_blue, a_red = idx(adc_feats, [p[0] for p in pairs], 2), idx(adc_feats, [p[0] for p in pairs], 3)
    s_blue, s_red = idx(sup_feats, [p[1] for p in pairs], 2), idx(sup_feats, [p[1] for p in pairs], 3)
    x = bot_features(w_adc[a_blue], w_sup[s_blue], w_adc[a_red], w_sup[s_red]) if n else np.zeros((0, 50))
    groups = _group_by_match(role_games)
    offset = np.array([sum(groups[p[0][0]][r][0] + groups[p[0][0]][r][1] for r in ("adc", "support")) for p in pairs])
    won = np.array([bool(p[0][4]) for p in pairs], dtype=bool)
    game = fit_bot_game(x, offset if n else np.zeros(0), won)
    with_gold = np.array([p[0][5] is not None and p[1][5] is not None for p in pairs], dtype=bool)
    gold_total = np.array([(p[0][5] or 0.0) + (p[1][5] or 0.0) for p in pairs], dtype=float)
    predicted = adc_pred[a_blue, a_red] + sup_pred[s_blue, s_red] if n else np.zeros(0)
    sigma2 = float(gold_total[with_gold].var()) if with_gold.sum() > 1 else DEFAULT_SIGMA2
    gold = fit_bot_gold(x[with_gold], predicted[with_gold], gold_total[with_gold], sigma2)
    out = []
    names = [f"{a}|{s}" for a in ARCHETYPES["adc"] for s in ARCHETYPES["support"]]
    size = len(names)
    for prefix, values, support in (("bot_game:", game, n), ("bot_gold:", gold, int(with_gold.sum()))):
        for kind, chunk in (("cross", values[:size]), ("pair", values[size:])):
            out += [MatchupFit(window=window, role=BOT_ROLE, param=f"{prefix}{kind}:{name}", value=float(v), support=support)
                    for name, v in zip(names, chunk)]
    return out


def _behind_rows(role: str, lanes: list, window: str) -> list:
    """Each champion's win rate when 300+ gold behind at 15 minutes, shrunk towards the role's own."""
    games: dict[int, float] = {}
    wins: dict[int, float] = {}
    for _, _, x, y, win, gold, w in lanes:
        if gold is None or abs(gold) < BEHIND_GOLD:
            continue
        trailing, won = (x, win) if gold < 0 else (y, 1 - win)
        games[trailing] = games.get(trailing, 0.0) + w
        wins[trailing] = wins.get(trailing, 0.0) + w * won
    total = sum(games.values())
    mean = sum(wins.values()) / total if total else 0.5
    return [MatchupFit(window=window, role=role, param=f"{BEHIND_PREFIX}{c}",
                       value=(wins[c] + BEHIND_K * mean) / (n + BEHIND_K), support=round(n)) for c, n in games.items()]


def _group_by_match(role_games: Mapping) -> dict:
    out: dict = {}
    for role, rows in role_games.items():
        for match_id, edge, gap, won in rows:
            out.setdefault(match_id, {})[role] = (edge, gap, won)
    return out


def _loo_edges(lanes: list, feats: RoleFeatures, coefs: np.ndarray, strength: np.ndarray, k: float) -> list:
    """(match, blue's game matchup beyond strength with this game left out, strength gap, blue won)."""
    n_c = len(feats.champs)
    games, wins = np.zeros((n_c, n_c)), np.zeros((n_c, n_c))
    for _, _, x, y, win, _gold, w in lanes:
        i, j = feats.index[x], feats.index[y]
        games[i, j] += w
        games[j, i] += w
        wins[i, j] += w * win
        wins[j, i] += w * (1 - win)
    labels_only = feats.matrix(coefs) if n_c else np.zeros((0, 0))
    out = []
    for match_id, _, x, y, win, _gold, w in lanes:
        i, j = feats.index[x], feats.index[y]
        gap = float(strength[i] - strength[j])
        prior = 1 / (1 + np.exp(-(gap + labels_only[i, j])))
        wr = (wins[i, j] - w * win + k * prior) / (games[i, j] - w + k)
        out.append((match_id, float(np.log(wr / (1 - wr))) - gap, gap, bool(win)))
    return out


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
        lane_pairs = _lane_pairs(conn, values, params)
        lanes = _lanes(conn, values, params)
    wr = {(r, c): (wins + 0.5 * STRENGTH_K) / (games + STRENGTH_K) for r, c, games, wins in roles}
    rows, out, role_games, bot_inputs = [], {}, {}, {}
    for role in ROLES:
        here = [p for p in pairs if p[0] == role]
        role_lanes = [r for r in lanes if r[1] == role]
        champs = sorted({c for p in here for c in p[1:3]} | {c for r, c in wr if r == role}
                        | {c for r in role_lanes for c in r[2:4]})
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
        lane_rows, gold_pred = _fit_lane(role, [p for p in lane_pairs if p[0] == role], role_lanes, feats, priors, window)
        rows += lane_rows
        by_champ = dict(zip(champs, strength))
        rows += _fit_slopes(role, role_lanes, by_champ, written, classes, window)
        role_games[role] = _loo_edges(role_lanes, feats, coefs, strength, k)
        rows += _behind_rows(role, role_lanes, window)
        bot_inputs[role] = (role_lanes, feats, gold_pred)
    rows += _fit_role_weights(role_games, window)
    rows += _fit_bot(role_games, bot_inputs, window)
    with Session(db) as s:
        s.exec(delete(MatchupFit).where(MatchupFit.window == window))
        s.add_all(rows)
        s.commit()
    return out

