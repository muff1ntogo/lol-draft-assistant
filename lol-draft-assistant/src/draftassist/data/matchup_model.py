"""The fitted matchup model, read back (docs/matchup-archetypes.md, Stages 3-5). Fitting: data/matchup_fit.py.

A pair's own wins and games, and the strength in its prior, come from the current patch and the player's bracket
(data/provider.py). The lane matchup pools every patch and bracket.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import Engine, text
from sqlmodel import Session, col, select

from draftassist.data.archetypes import (
    ARCHETYPES,
    resolve_archetype,
)
from draftassist.data.synergy import DECAY_PATCHES, crawl_weights, latest_window, weights_cte
from draftassist.data.tags import load_archetypes, load_kit
from draftassist.db.models import Champion, MatchupFit
from draftassist.engine.bot_lane import BotLane
from draftassist.engine.matchup_model import (
    DEFAULT_K,
    DEFAULT_SLOPE,
    PP_PER_LOGODDS,
    RoleFeatures,
    interactions_for,
    param_names,
    smooth,
)
from draftassist.engine.matchup_model import lane_strength as _lane_strength
from draftassist.engine.model import ROLES, BracketStats, RoleStat
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
MIN_EXPLAINED = 0.002  # log-odds (0.05pp): a term below this doesn't explain a matchup
BOT_ROLE = "bot"  # Stage 5: bot_game:cross:a|s, bot_game:pair:a|s, bot_gold:cross:a|s, bot_gold:pair:a|s
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
    _feats: dict = field(default_factory=dict)
    half_life: float = DECAY_PATCHES  # of the observed matchups and lane leads; the fitted terms keep their own window

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
                    out[(role, a, b)] = smooth(gold, n, k, prior)
        self._lane_table = out
        return out

    def features(self, role: str) -> RoleFeatures:
        if role not in self._feats:
            self._feats[role] = _features(role, sorted(self.classes), self.written, self.classes, self.kit)
        return self._feats[role]

    def labels_table(self) -> dict[tuple[int, str], str]:
        return {(c, r): self.label(r, c).primary for r in ROLES for c in self.classes}

    def explain(self, role: str, a: int, b: int) -> tuple[str, float] | None:
        """a's biggest fitted term against b: (parameter, log-odds), or None if nothing favours a."""
        feats = self.features(role)
        if a not in feats.index or b not in feats.index:
            return None
        row = feats.pair_features(np.array([feats.index[a]]), np.array([feats.index[b]]))[0]
        values = row * np.array([self.coefs(role).get(n, 0.0) for n in feats.names])
        at = int(values.argmax())
        return (feats.names[at], float(values[at])) if values[at] > MIN_EXPLAINED else None

    def expected(self, role: str, a: str, b: str) -> float:
        """Archetype a against b, in pp, from the fit."""
        coefs = self.coefs(role)
        value = coefs.get(f"E:{a}>{b}")
        return (value if value is not None else -coefs.get(f"E:{b}>{a}", 0.0)) * PP_PER_LOGODDS

    def prediction(self, role: str) -> tuple[Mapping[int, int], np.ndarray]:
        """(champ -> index, label-only log-odds of row vs column) for every champion, built once per role."""
        if role not in self._matrices:
            feats = self.features(role)
            coefs = np.array([self.coefs(role).get(n, 0.0) for n in feats.names])
            self._matrices[role] = (feats.index, feats.matrix(coefs))
        return self._matrices[role]


def load_matchup_model(db: Engine, window: str | None = None, half_life: float = DECAY_PATCHES) -> MatchupModel | None:
    """The stored fit of `window` (default: the latest), or None before the first fit. `half_life` decays the observed
    lane leads and `pooled_stats`, for the Matchup tree's own setting; the fit itself isn't refitted."""
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
        values, params = weights_cte(crawl_weights(conn, window, half_life))
        pairs = _lane_pairs(conn, values, params)
    return MatchupModel(window, coefs, ks, written, classes, kit, {}, lane_coefs, lane_ks, slopes, weights, pairs, None, bot,
                        half_life=half_life)


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
                matchups[(role, a, b)] = smooth(wins, n, k, float(prior[i, j]))
                if n:
                    games[(role, a, b)] = n
    return matchups, games


def pooled_stats(db: Engine, model: MatchupModel, bracket: str) -> BracketStats:
    """Matchups over the whole window, for the Matchup tree: every crawled patch, decayed, and every tier for the sample
    size, with pick rates from `bracket`. Patches decay by the model's half-life. Live stays on the current patch."""
    with db.connect() as conn:
        values, params = weights_cte(crawl_weights(conn, model.window, model.half_life))
        cte = f"WITH w(patch, weight) AS (VALUES {values}) "
        rows = conn.execute(text(
            cte + "SELECT role, champ_a, champ_b, SUM(games * weight) AS games, SUM(wins_a * weight) AS wins_a "
            "FROM matchups JOIN w USING (patch) GROUP BY role, champ_a, champ_b"), params).all()
        roles = conn.execute(text(
            cte + "SELECT role, champ_id, SUM(games * weight), SUM(wins * weight), "
            "SUM(CASE WHEN bracket = :bracket THEN picks * weight ELSE 0 END) "
            "FROM champ_role_stats JOIN w USING (patch) GROUP BY role, champ_id"), {**params, "bracket": bracket}).all()
        matches = conn.execute(text(
            cte + "SELECT SUM(matches * weight) FROM bracket_totals JOIN w USING (patch) WHERE bracket = :bracket"),
            {**params, "bracket": bracket}).scalar() or 0.0
    meta: dict[int, dict[str, RoleStat]] = {}
    strength: dict[tuple[int, str], float] = {}
    for role, champ, games, wins, picks in roles:
        wr = (wins + 0.5 * STRENGTH_K) / (games + STRENGTH_K)
        strength[(champ, role)] = float(np.log(wr / (1 - wr)))
        meta.setdefault(champ, {})[role] = RoleStat(wr, picks / matches if matches else 0.0)
    matchups, games = smoothed_matchups(model, meta, rows, strength)
    return BracketStats(meta, matchups, {}, True, {k: round(n) for k, n in games.items()},
                        {r: model.k(r) for r in ROLES}, strength, model.lane_table(), model.lane_slopes())


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
