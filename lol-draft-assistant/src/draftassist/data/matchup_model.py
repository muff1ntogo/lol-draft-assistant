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
from sqlmodel import Session, col, delete, select

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
from draftassist.engine.bot_lane import BotLane, bot_features, fit_bot_game, fit_bot_gold
from draftassist.engine.matchup_model import (
    DEFAULT_K,
    DEFAULT_SLOPE,
    PP_PER_LOGODDS,
    PRIOR_SD_PP,
    RoleFeatures,
    empirical_k,
    GOLD_PER_PP,
    empirical_k_gold,
    fit_lane_gold,
    fit_lane_slopes,
    fit_role,
    fit_role_weights,
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
# Lane (Stage 4): the mean gold lead at 15 minutes. A champion's lane strength is stable, so the lane matchup pools every
# patch and bracket. Parameters in matchup_fits: lane:E:*, lane:K:* (gold), lane:k_r, slope:<archetype>, role_weight(_se).
LANE_PREFIX = "lane:"
LANE_K = LANE_PREFIX + K_PARAM
SLOPE_PREFIX = "slope:"
ROLE_WEIGHT, ROLE_WEIGHT_SE = "role_weight", "role_weight_se"
BEHIND_PREFIX = "behind:"  # Stage 6: win rate when BEHIND_GOLD+ behind at 15 minutes
BEHIND_GOLD = 300
BEHIND_K = 50
BOT_ROLE = "bot"  # Stage 5: bot_game:cross:a|s, bot_game:pair:a|s, bot_gold:cross:a|s, bot_gold:pair:a|s
LANE_STRENGTH_K = 100  # a champion's own mean lead shrinks towards 0 by this many lanes
LANE_MIN_GAMES = 30  # champions with fewer lane games in the window get no lane matchups
DEFAULT_SIGMA2 = 1000.0 ** 2  # one lane's variance in gold^2, when there's no data to measure it


def _labels(db: Engine) -> tuple[dict, dict, KitTraits]:
    with Session(db) as s:
        classes = {c.champ_id: c.tags for c in s.exec(select(Champion)).all()}
    return load_archetypes(db), classes, KitTraits(load_kit(db))


def _features(role: str, champs: list[int], written: Mapping, classes: Mapping, kit: KitTraits) -> RoleFeatures:
    labels = {c: resolve_archetype(written, c, role, classes.get(c)) for c in champs}
    return RoleFeatures(role, ARCHETYPES[role], champs, labels, kit)


def _lane_pairs(conn, values: str, params: dict) -> list:
    """(role, a, b, lanes, a's summed gold lead): pooled over the window."""
    return conn.execute(text(
        f"WITH w(patch, weight) AS (VALUES {values}) "
        "SELECT role, champ_a, champ_b, SUM(lane_n * weight), SUM(lane_gold_sum * weight) "
        "FROM matchups JOIN w USING (patch) WHERE lane_n > 0 GROUP BY role, champ_a, champ_b"), params).all()


def _lane_strength(pairs: list, champs: list[int]) -> np.ndarray:
    """Each champion's mean gold lead against everyone, shrunk towards 0."""
    lanes = dict.fromkeys(champs, 0.0)
    gold = dict.fromkeys(champs, 0.0)
    for _, a, b, n, g in pairs:
        lanes[a], gold[a] = lanes[a] + n, gold[a] + g
        lanes[b], gold[b] = lanes[b] + n, gold[b] - g
    return np.array([gold[c] / (lanes[c] + LANE_STRENGTH_K) for c in champs])


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


def _fit_slopes(role: str, lanes: list, strength: Mapping, labels: Mapping, window: str) -> list:
    """Each lane twice, from both champions' sides: each lead is worth its own archetype's slope."""
    words = ARCHETYPES[role]
    at = {w: i for i, w in enumerate(words)}
    rows = [r for r in lanes if r[5] is not None]
    n = len(rows)
    arch = np.zeros(2 * n, dtype=int)
    lead, offset = np.zeros(2 * n), np.zeros(2 * n)
    won = np.zeros(2 * n, dtype=bool)
    for i, (_, _, x, y, win, gold, _w) in enumerate(rows):
        gap = strength.get(x, 0.0) - strength.get(y, 0.0)
        arch[i], lead[i], offset[i], won[i] = at[labels(x).primary], gold / 1000, gap, bool(win)
        j = n + i
        arch[j], lead[j], offset[j], won[j] = at[labels(y).primary], -gold / 1000, -gap, not win
    slopes = fit_lane_slopes(arch, lead, offset, won, len(words))
    return [MatchupFit(window=window, role=role, param=SLOPE_PREFIX + w, value=float(s), support=int((arch == i).sum()))
            for i, (w, s) in enumerate(zip(words, slopes))]


def _fit_role_weights(role_games: Mapping, window: str) -> list:
    """Leave-one-out edges: a game can't be predicted from a pair record that includes it."""
    complete = [m for m, by in _group_by_match(role_games).items() if len(by) == len(ROLES)]
    grouped = _group_by_match(role_games)
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
        rows += _fit_slopes(role, role_lanes, by_champ,
                            lambda c: resolve_archetype(written, c, role, classes.get(c)), window)
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


@dataclass
class MatchupModel:
    window: str
    _coefs: Mapping[str, Mapping[str, float]]
    _k: Mapping[str, float]
    written: Mapping
    classes: Mapping
    kit: KitTraits
    _matrices: dict = field(default_factory=dict)
    _lane_coefs: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    _lane_k: Mapping[str, float] = field(default_factory=dict)
    _slopes: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    _role_weights: Mapping[str, float] = field(default_factory=dict)
    _lane_pairs: list = field(default_factory=list)
    _lane_table: dict | None = None
    _bot: Mapping[str, float] = field(default_factory=dict)

    def coefs(self, role: str) -> Mapping[str, float]:
        return self._coefs.get(role, {})

    def k(self, role: str) -> float:
        return self._k.get(role, DEFAULT_K)

    def lane_k(self, role: str) -> float:
        return self._lane_k.get(role, DEFAULT_K)

    def slope(self, role: str, archetype: str) -> float:
        return self._slopes.get(role, {}).get(archetype, DEFAULT_SLOPE)

    def role_weights(self) -> Mapping[str, float]:
        return self._role_weights

    def label(self, role: str, champ: int):
        return resolve_archetype(self.written, champ, role, self.classes.get(champ))

    def lane_slopes(self) -> dict[tuple[int, str], float]:
        """(champ, role) -> what a lead is worth to it, from its main archetype."""
        return {(c, r): self.slope(r, self.label(r, c).primary) for r in ROLES for c in self.classes}

    def bot(self) -> BotLane:
        """The 2v2 tables, with every champion's archetype weights as they stand in each bot role."""
        adc_words, sup_words = ARCHETYPES["adc"], ARCHETYPES["support"]

        def table(prefix: str) -> np.ndarray:
            return np.array([[self._bot.get(f"{prefix}:{a}|{s}", 0.0) for s in sup_words] for a in adc_words])

        adc, sup = self.features("adc"), self.features("support")
        slopes = [self.slope(r, w) for r in ("adc", "support") for w in ARCHETYPES[r]]
        labels = {(c, r): self.label(r, c).primary for r in ("adc", "support") for c in self.classes}
        return BotLane({c: adc.weights[i] for c, i in adc.index.items()}, {c: sup.weights[i] for c, i in sup.index.items()},
                       table("bot_game:cross"), table("bot_game:pair"), table("bot_gold:cross"), table("bot_gold:pair"),
                       labels, sum(slopes) / len(slopes))

    def lane_table(self) -> dict[tuple[str, int, int], float]:
        """a's expected gold lead at 15 minutes per pair, (role, a, b) with a < b, for every pair of champions with enough
        lane games. Shared by every bracket: pooled, built once."""
        if self._lane_table is not None:
            return self._lane_table
        out: dict[tuple[str, int, int], float] = {}
        for role in ROLES:
            pairs = [p for p in self._lane_pairs if p[0] == role]
            if not pairs:
                continue
            everyone = sorted(self.classes)
            lanes = dict.fromkeys(everyone, 0.0)
            for _, a, b, n, _g in pairs:
                if a in lanes and b in lanes:
                    lanes[a] += n
                    lanes[b] += n
            pairs = [p for p in pairs if p[1] in lanes and p[2] in lanes]
            feats = self.features(role)
            coefs = np.array([self._lane_coefs.get(role, {}).get(n, 0.0) for n in feats.names])
            labels_only = feats.matrix(coefs)
            strength = _lane_strength(pairs, feats.champs)
            k = self.lane_k(role)
            met = {(p[1], p[2]): (p[4], p[3]) for p in pairs}  # (summed lead, lanes)
            champs = [c for c in everyone if lanes[c] >= LANE_MIN_GAMES]
            for i, a in enumerate(champs):
                ia = feats.index[a]
                for b in champs[i + 1:]:
                    ib = feats.index[b]
                    prior = float(strength[ia] - strength[ib] + labels_only[ia, ib])
                    gold, n = met.get((a, b), (0.0, 0.0))
                    out[(role, a, b)] = (gold + k * prior) / (n + k)
        self._lane_table = out
        return out

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
    lane_coefs: dict[str, dict[str, float]] = {}
    slopes: dict[str, dict[str, float]] = {}
    ks: dict[str, float] = {}
    lane_ks: dict[str, float] = {}
    weights: dict[str, float] = {}
    bot: dict[str, float] = {}
    for r in rows:
        if r.role == BOT_ROLE:
            bot[r.param] = r.value
        elif r.param == K_PARAM:
            ks[r.role] = r.value
        elif r.param == LANE_K:
            lane_ks[r.role] = r.value
        elif r.param == ROLE_WEIGHT:
            weights[r.role] = r.value
        elif r.param == ROLE_WEIGHT_SE or r.param.startswith(BEHIND_PREFIX):
            continue
        elif r.param.startswith(LANE_PREFIX):
            lane_coefs.setdefault(r.role, {})[r.param[len(LANE_PREFIX):]] = r.value
        elif r.param.startswith(SLOPE_PREFIX):
            slopes.setdefault(r.role, {})[r.param[len(SLOPE_PREFIX):]] = r.value
        else:
            coefs.setdefault(r.role, {})[r.param] = r.value
    written, classes, kit = _labels(db)
    with db.connect() as conn:
        values, params = weights_cte(crawl_weights(conn, window))
        pairs = _lane_pairs(conn, values, params)
    return MatchupModel(window, coefs, ks, written, classes, kit, {}, lane_coefs, lane_ks, slopes, weights, pairs, None, bot)


def load_lane_slope(db: Engine, role: str, window: str | None = None) -> float:
    """The role's mean slope per 1,000 gold, without loading the whole model (History's lane bars)."""
    window = window or latest_window(db)
    if window is None:
        return DEFAULT_SLOPE
    with Session(db) as s:
        rows = s.exec(select(MatchupFit).where(MatchupFit.window == window, MatchupFit.role == role,
                                               col(MatchupFit.param).startswith(SLOPE_PREFIX))).all()
    return sum(r.value for r in rows) / len(rows) if rows else DEFAULT_SLOPE


def load_role_weights(db: Engine, window: str | None = None) -> dict[str, float]:
    """The fitted weight of each role's matchups, without loading the whole model. Empty before the first fit."""
    window = window or latest_window(db)
    if window is None:
        return {}
    with Session(db) as s:
        rows = s.exec(select(MatchupFit).where(MatchupFit.window == window, MatchupFit.param == ROLE_WEIGHT)).all()
    return {r.role: r.value for r in rows}


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
        weight, se = fits.get(ROLE_WEIGHT, (1.0, None, 0))[0], fits.get(ROLE_WEIGHT_SE, (0.0, None, 0))[0]
        lines.append(f"\n{role.upper()}  k_r {model.k(role):.0f}, lane k_r {model.lane_k(role):.0f}  ({support:,} weighted "
                     f"games); role weight {weight:.2f} +/- {se:.2f}")
        lines.append("  what a 1,000 gold lead is worth (pp): " + ", ".join(
            f"{w} {model.slope(role, w) * PP_PER_LOGODDS:.1f}" for w in ARCHETYPES[role]))
        index, labels_only = model.prediction(role)
        primary = {c: resolve_archetype(model.written, c, role, model.classes.get(c)).primary for c in index}
        for name in param_names(ARCHETYPES[role], interactions_for(ARCHETYPES[role])):
            value, prior, _ = fits.get(name, (0.0, 0.0, 0))
            lane = fits.get(LANE_PREFIX + name, (0.0, 0.0, 0))[0]
            if name.startswith("E:"):
                a, b = name[2:].split(">")
                ia = [index[c] for c, p in primary.items() if p == a]
                ib = [index[c] for c, p in primary.items() if p == b]
                typical = float(labels_only[np.ix_(ia, ib)].mean()) if ia and ib else 0.0
                lines.append(f"  {a} v {b}: {typical * PP_PER_LOGODDS:+.2f} [{prior or 0:+.2f}]  lane {lane:+.0f} gold")
            else:
                lines.append(f"  kit {name[2:]}: {value * PP_PER_LOGODDS:+.2f}  lane {lane:+.0f} gold")
    fits = _report_fits(db, model.window, BOT_ROLE)
    lines.append(f"\nBOT LANE 2v2  ({fits.get('bot_game:cross:scaler|engage', (0, None, 0))[2]:,} matches, "
                 f"{fits.get('bot_gold:cross:scaler|engage', (0, None, 0))[2]:,} with gold); the largest terms, pp [gold]")
    for kind, what in (("cross", "ADC v enemy support"), ("pair", "ADC with support")):
        cells = sorted(((name.split(":")[2], v[0]) for name, v in fits.items() if name.startswith(f"bot_game:{kind}:")),
                       key=lambda kv: -abs(kv[1]))[:5]
        lines.append(f"  {what}: " + ", ".join(
            f"{n.replace('|', ' / ')} {v * PP_PER_LOGODDS:+.2f} [{fits.get(f'bot_gold:{kind}:{n}', (0.0,))[0]:+.0f}]"
            for n, v in cells))
    return "\n".join(lines)


def _report_fits(db: Engine, window: str, role: str) -> dict[str, tuple[float, float | None, int]]:
    with Session(db) as s:
        rows = s.exec(select(MatchupFit).where(MatchupFit.window == window, MatchupFit.role == role)).all()
    return {r.param: (r.value, r.prior_pp, r.support) for r in rows}
