"""Fitting and reading back the expectations and the state model (docs/draft-vs-execution.md, Stage 2). The sums are in
engine/execution.py.

Every game's expected values come from models fitted on the other folds, so a game never predicts itself, and one fold never
trains the state model: it gives the Brier comparison. Tiers are pooled with a bracket adjustment (decision 3), because a
tier alone is too thin. Master+ lives in another database and doesn't reach this (decision 6).

CLI (a batch job, a few minutes on a full crawl, not part of aggregation):
    uv run python -m draftassist.data.execution_model
"""
from __future__ import annotations

import json
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import Engine, text
from sqlmodel import Session, select

from draftassist.data.archetypes import PRIORS_FILE, load_archetype_priors, priors_for
from draftassist.data.crawler import current_patch
from draftassist.data.matchup_model import _features, _labels
from draftassist.data.profiles import load_profiles
from draftassist.data.synergy import crawl_weights
from draftassist.db.models import ExecutionFit, MatchDraftLogOdds
from draftassist.engine.bot_lane import bot_features, fit_bot_gold
from draftassist.engine.comp import team_comp
from draftassist.engine.execution import (
    STATE_FEATURES,
    ChampMeans,
    Expect,
    LaneTarget,
    PoissonMatchups,
    SdTable,
    StateModel,
    brier,
    fit_draft_scale,
    fit_ridge,
    fit_state,
    pair_stat_leak,
    reliability_table,
    state_row,
)
from draftassist.engine.matchup_model import GOLD_PER_PP, LANE_PRIOR_SD_GOLD
from draftassist.engine.mathx import sigmoid
from draftassist.engine.model import ROLES

FOLDS = 5
HELD_OUT = 0  # this fold never trains the state model
OBJECTIVES = ("towers", "plates", "dragons", "grubs", "herald")
OBJECTIVE_PENALTY = 20.0  # ridge on junglers: a rare one stays near nothing
GOLD_25_PENALTY = 1e-3
LANE_TARGETS = {"gold": ("lane_gold_15", LANE_PRIOR_SD_GOLD), "xp": ("lane_xp_15", LANE_PRIOR_SD_GOLD),
                "cs": ("lane_cs_14", 8.0)}  # priors: about 150 gold's worth of each
BOT_TARGETS = ("gold", "xp")
LANES = ("top", "jungle", "mid")  # the 1v1s; bot is 2v2
MINUTES_15 = 15.0


# ---------- the games ----------
@dataclass
class Game:
    match_id: str
    bracket: str
    patch: str
    won: bool  # blue
    blue: dict[str, int]
    red: dict[str, int]
    players: dict[int, dict[str, float | None]]  # champ -> columns
    state: dict[str, float | None]  # blue minus red: gold, gold_25 and each objective
    early_blue: float = 0.0
    early_red: float = 0.0
    fold: int = 0
    draft: float = 0.0

    @property
    def early_diff(self) -> float:
        return self.early_blue - self.early_red


_PLAYER_COLUMNS = ("lane_gold_15", "lane_xp_15", "lane_cs_14", "jungle_cs_15", "kp_15", "deaths_solo_14", "deaths_ganked_14",
                   "wards_placed_15", "wards_killed_15")
_TEAM_COLUMNS = {"gold": "gold_15", "gold_25": "gold_25", **{o: f"{o}_15" for o in OBJECTIVES}}


def fold_of(match_id: str) -> int:
    return zlib.crc32(match_id.encode()) % FOLDS


def load_games(db: Engine, profiles) -> list[Game]:
    """Crawled matches with everything from their timeline, 15 minutes or longer, five lanes a side."""
    with db.connect() as conn:
        meta = {m: (b, p) for m, b, p in conn.execute(text(
            "SELECT match_id, bracket, patch FROM crawled_matches WHERE timeline_version >= 2"))}
        cols = ", ".join(_PLAYER_COLUMNS)
        players: dict[str, list] = {}
        for row in conn.execute(text(
                f"SELECT p.match_id, p.team_id, p.role, p.champ_id, p.win, {cols} FROM match_participants p "
                "JOIN crawled_matches m ON m.match_id = p.match_id WHERE m.timeline_version >= 2")):
            players.setdefault(row[0], []).append(row)
        team_cols = ", ".join(dict.fromkeys(_TEAM_COLUMNS.values()))
        teams: dict[str, dict] = {}
        for row in conn.execute(text(f"SELECT match_id, team_id, {team_cols} FROM match_team_state")):
            teams.setdefault(row[0], {})[row[1]] = dict(zip(dict.fromkeys(_TEAM_COLUMNS.values()), row[2:]))
    games = []
    for match_id, (bracket, patch) in meta.items():
        rows, team = players.get(match_id, []), teams.get(match_id, {})
        if len(rows) != 10 or len(team) != 2:
            continue
        blue, red, by_champ, won = {}, {}, {}, None
        for _, team_id, role, champ, win, *values in rows:
            (blue if team_id == 100 else red)[role] = champ
            by_champ[champ] = dict(zip(_PLAYER_COLUMNS, values))
            if team_id == 100:
                won = bool(win)
        if len(blue) != 5 or len(red) != 5 or any(v["lane_gold_15"] is None for v in by_champ.values()):
            continue
        state = {}
        for name, column in _TEAM_COLUMNS.items():
            a, b = team[100].get(column), team[200].get(column)
            state[name] = None if a is None or b is None else a - b
        if any(state[n] is None for n in ("gold", *OBJECTIVES)):
            continue
        early_b, early_r = _early(profiles, blue), _early(profiles, red)
        games.append(Game(match_id, bracket, patch, won, blue, red, by_champ, state, early_b, early_r, fold_of(match_id)))
    games.sort(key=lambda g: g.match_id)
    return games


def _early(profiles, comp: Mapping[str, int]) -> float:
    """How early a comp is: its power curve, late games over short ones, turned around. 0 with no data."""
    curve = team_comp(profiles, [(c, r) for r, c in comp.items()]).curve
    return -curve if curve is not None else 0.0


# ---------- the expectations ----------
@dataclass
class Labels:
    """What a role's features are built from, so a stored model can rebuild them."""
    written: Mapping
    classes: Mapping
    kit: object
    cache: dict = field(default_factory=dict)

    def features(self, role: str, champs: list[int]):
        key = (role, tuple(champs))
        if key not in self.cache:
            self.cache[key] = _features(role, champs, self.written, self.classes, self.kit)
        return self.cache[key]


@dataclass
class Expectations:
    lane: dict[str, dict[str, LaneTarget]]  # target -> role
    bot: dict[str, np.ndarray]  # target -> the 2v2 coefficients
    jungle_cs: ChampMeans
    kp: ChampMeans
    vision: ChampMeans
    solo: dict[str, PoissonMatchups]  # role -> victim against their lane opponent
    ganked: dict[str, PoissonMatchups]  # role -> victim against the enemy jungler
    objectives: dict[str, dict]  # name -> {blue, early, jungler: {champ: effect}}
    gold_25: np.ndarray

    def lane_mean(self, target: str, role: str, a: int, b: int) -> float:
        return self.lane[target][role].expected(a, b)

    def bot_mean(self, target: str, adc: int, sup: int, e_adc: int, e_sup: int) -> float:
        """The two bot players' leads together: both 1v1s plus what the pairings add."""
        base = self.lane[target]["adc"].expected(adc, e_adc) + self.lane[target]["support"].expected(sup, e_sup)
        a_feats, s_feats = self.lane[target]["adc"].feats, self.lane[target]["support"].feats
        if any(c not in a_feats.index for c in (adc, e_adc)) or any(c not in s_feats.index for c in (sup, e_sup)):
            return base
        x = bot_features(a_feats.weights[[a_feats.index[adc]]], s_feats.weights[[s_feats.index[sup]]],
                         a_feats.weights[[a_feats.index[e_adc]]], s_feats.weights[[s_feats.index[e_sup]]])
        return float(base + (x @ self.bot[target])[0])

    def objective_mean(self, name: str, j_blue: int, j_red: int, early_blue: float, early_red: float) -> float:
        o = self.objectives[name]
        return float(o["blue"] + o["early"] * (early_blue - early_red) + o["jungler"].get(j_blue, 0.0) - o["jungler"].get(j_red, 0.0))

    def gold_mean(self, blue: Mapping[str, int], red: Mapping[str, int]) -> float:
        return (sum(self.lane_mean("gold", r, blue[r], red[r]) for r in LANES)
                + self.bot_mean("gold", blue["adc"], blue["support"], red["adc"], red["support"]))

    def gold_25_mean(self, gold_15: float, early_blue: float, early_red: float) -> float:
        return float(_gold_25_row(gold_15, early_blue, early_red) @ self.gold_25)


def _gold_25_row(gold_15: float, early_blue: float, early_red: float) -> np.ndarray:
    g = gold_15 / 1000
    return np.array([g, early_blue - early_red, g * (early_blue + early_red)])


def _fit_expectations(games: list[Game], labels: Labels, role_champs: Mapping[str, list[int]], priors: Mapping) -> Expectations:
    lane: dict[str, dict[str, LaneTarget]] = {}
    for target, (column, prior_sd) in LANE_TARGETS.items():
        lane[target] = {}
        for role in ROLES:
            feats = labels.features(role, role_champs[role])
            rows = [(g.blue[role], g.red[role], v) for g in games
                    if (v := g.players[g.blue[role]][column]) is not None]
            wanted = priors_for(priors, role, "lane") if target == "gold" else {}
            prior = np.array([wanted.get(tuple(n[2:].split(">")), 0.0) * GOLD_PER_PP if n.startswith("E:") else 0.0
                              for n in feats.names])
            lane[target][role] = LaneTarget.fit(feats, rows, prior, prior_sd)

    bot = {}
    for target in BOT_TARGETS:
        column = LANE_TARGETS[target][0]
        adc, sup = lane[target]["adc"], lane[target]["support"]
        used = [g for g in games if g.players[g.blue["adc"]][column] is not None and g.players[g.blue["support"]][column] is not None]
        if used:
            pick = lambda feats, side, role: feats.weights[[feats.index[getattr(g, side)[role]] for g in used]]  # noqa: E731, B023
            x = bot_features(pick(adc.feats, "blue", "adc"), pick(sup.feats, "blue", "support"),
                             pick(adc.feats, "red", "adc"), pick(sup.feats, "red", "support"))
            y = np.array([g.players[g.blue["adc"]][column] + g.players[g.blue["support"]][column] for g in used])
            offset = np.array([adc.expected(g.blue["adc"], g.red["adc"]) + sup.expected(g.blue["support"], g.red["support"])
                               for g in used])
            bot[target] = fit_bot_gold(x, offset, y, max(float(np.var(y)), 1e-6))
        else:
            bot[target] = np.zeros(2 * len(adc.feats.words) * len(sup.feats.words))

    def means(role: str, value) -> ChampMeans:
        keys, values, brackets = [], [], []
        for g in games:
            for side in (g.blue, g.red):
                v = value(g.players[side[role]])
                if v is not None:
                    keys.append(side[role])
                    values.append(v)
                    brackets.append(g.bracket)
        return ChampMeans.fit(keys, values, brackets)

    def deaths(role: str, column: str, killer_role: str) -> PoissonMatchups:
        rows, brackets = [], []
        for g in games:
            for own, enemy in ((g.blue, g.red), (g.red, g.blue)):
                v = g.players[own[role]][column]
                if v is not None:
                    rows.append((own[role], enemy[killer_role], float(v)))
                    brackets.append(g.bracket)
        return PoissonMatchups.fit(rows, brackets)

    objectives = {}
    junglers = sorted({c for g in games for c in (g.blue["jungle"], g.red["jungle"])})
    at = {c: i + 2 for i, c in enumerate(junglers)}
    x = np.zeros((len(games), len(junglers) + 2))
    x[:, 0] = 1.0
    for i, g in enumerate(games):
        x[i, 1] = g.early_diff
        x[i, at[g.blue["jungle"]]] += 1.0
        x[i, at[g.red["jungle"]]] -= 1.0
    for name in OBJECTIVES:
        beta = fit_ridge(x, np.array([g.state[name] for g in games], dtype=float), OBJECTIVE_PENALTY)
        objectives[name] = {"blue": float(beta[0]), "early": float(beta[1]),
                            "jungler": {c: float(beta[at[c]]) for c in junglers}}

    long_games = [g for g in games if g.state["gold_25"] is not None]
    if long_games:
        rows_25 = np.array([_gold_25_row(g.state["gold"], g.early_blue, g.early_red) for g in long_games])
        gold_25 = fit_ridge(rows_25, np.array([g.state["gold_25"] for g in long_games], dtype=float), GOLD_25_PENALTY)
    else:
        gold_25 = np.zeros(3)

    return Expectations(
        lane, bot, means("jungle", lambda p: p["jungle_cs_15"]), means("jungle", lambda p: p["kp_15"]),
        means("support", _vision),
        {r: deaths(r, "deaths_solo_14", r) for r in ROLES}, {r: deaths(r, "deaths_ganked_14", "jungle") for r in ROLES},
        objectives, gold_25)


def _vision(player: Mapping) -> float | None:
    placed, killed = player["wards_placed_15"], player["wards_killed_15"]
    return None if placed is None or killed is None else (placed + killed) / MINUTES_15


# ---------- residuals: how far games land from what's expected ----------
def _residuals(ex: Expectations, g: Game) -> list[tuple[str, str, float]]:
    """(stat, key, actual minus expected) for one game the expectations weren't fitted on."""
    out = []
    for target, (column, _) in LANE_TARGETS.items():
        for role in ROLES:
            b, r = g.blue[role], g.red[role]
            v = g.players[b][column]
            if v is not None:
                res = v - ex.lane_mean(target, role, b, r)
                out += [(f"lane:{target}", f"{role}|{b}", res), (f"lane:{target}", f"{role}|{r}", -res)]
    for target in BOT_TARGETS:
        column = LANE_TARGETS[target][0]
        a, s = g.players[g.blue["adc"]][column], g.players[g.blue["support"]][column]
        if a is not None and s is not None:
            out.append((f"bot:{target}", "", a + s - ex.bot_mean(target, g.blue["adc"], g.blue["support"], g.red["adc"], g.red["support"])))
    for side in (g.blue, g.red):
        j, sup = side["jungle"], side["support"]
        for stat, means, value in (("jungle_cs", ex.jungle_cs, g.players[j]["jungle_cs_15"]), ("kp", ex.kp, g.players[j]["kp_15"])):
            if value is not None:
                out.append((stat, str(j), value - means.mean(j, g.bracket)))
        v = _vision(g.players[sup])
        if v is not None:
            out.append(("vision", str(sup), v - ex.vision.mean(sup, g.bracket)))
    for own, enemy in ((g.blue, g.red), (g.red, g.blue)):
        for role in ROLES:
            c = own[role]
            solo, ganked = g.players[c]["deaths_solo_14"], g.players[c]["deaths_ganked_14"]
            if solo is not None:
                out.append(("solo", f"{role}|{c}", solo - ex.solo[role].mean(c, enemy[role], g.bracket)))
            if ganked is not None:
                out.append(("ganked", f"{role}|{c}", ganked - ex.ganked[role].mean(c, enemy["jungle"], g.bracket)))
    for name in OBJECTIVES:
        out.append((f"objective:{name}", "", g.state[name] - ex.objective_mean(name, g.blue["jungle"], g.red["jungle"], g.early_blue, g.early_red)))
    out.append(("team_gold", "", g.state["gold"] - ex.gold_mean(g.blue, g.red)))
    if g.state["gold_25"] is not None:
        out.append(("gold_25", "", g.state["gold_25"] - ex.gold_25_mean(g.state["gold"], g.early_blue, g.early_red)))
    return out


def _state_residual(ex: Expectations, g: Game) -> np.ndarray:
    return _row_from_state(ex, g.state, g.blue, g.red, g.early_blue, g.early_red)


def _row_from_state(ex: Expectations, state: Mapping, blue: Mapping, red: Mapping, early_blue: float, early_red: float) -> np.ndarray:
    expected = {name: ex.objective_mean(name, blue["jungle"], red["jungle"], early_blue, early_red) for name in OBJECTIVES}
    return state_row(gold=(state["gold"] - ex.gold_mean(blue, red)) / 1000,
                     **{name: state[name] - expected[name] for name in OBJECTIVES},
                     early_blue=early_blue, early_red=early_red)


# ---------- the whole model ----------
@dataclass
class ExecutionModel:
    window: str
    expectations: Expectations
    sds: dict[str, SdTable]
    bracket_scale: dict[str, dict[str, float]]
    draft_scale: float
    state: StateModel
    report: dict

    def _expect(self, stat: str, mean: float, key=None, bracket: str | None = None) -> Expect:
        return Expect(mean, self.sds[stat].sd(key) * self.bracket_scale.get(stat, {}).get(bracket, 1.0))

    def lane(self, target: str, role: str, a: int, b: int, bracket: str | None = None) -> Expect:
        return self._expect(f"lane:{target}", self.expectations.lane_mean(target, role, a, b), f"{role}|{a}", bracket)

    def bot(self, target: str, adc: int, sup: int, e_adc: int, e_sup: int, bracket: str | None = None) -> Expect:
        return self._expect(f"bot:{target}", self.expectations.bot_mean(target, adc, sup, e_adc, e_sup), None, bracket)

    def jungle_cs(self, champ: int, bracket: str | None = None) -> Expect:
        return self._expect("jungle_cs", self.expectations.jungle_cs.mean(champ, bracket), champ, bracket)

    def kp(self, champ: int, bracket: str | None = None) -> Expect:
        return self._expect("kp", self.expectations.kp.mean(champ, bracket), champ, bracket)

    def vision(self, champ: int, bracket: str | None = None) -> Expect:
        return self._expect("vision", self.expectations.vision.mean(champ, bracket), champ, bracket)

    def deaths_solo(self, role: str, champ: int, opponent: int, bracket: str | None = None) -> Expect:
        return self._expect("solo", self.expectations.solo[role].mean(champ, opponent, bracket), f"{role}|{champ}", bracket)

    def deaths_ganked(self, role: str, champ: int, enemy_jungler: int, bracket: str | None = None) -> Expect:
        return self._expect("ganked", self.expectations.ganked[role].mean(champ, enemy_jungler, bracket), f"{role}|{champ}", bracket)

    def objective(self, name: str, j_blue: int, j_red: int, early_blue: float, early_red: float, bracket: str | None = None) -> Expect:
        mean = self.expectations.objective_mean(name, j_blue, j_red, early_blue, early_red)
        return self._expect(f"objective:{name}", mean, None, bracket)

    def gold_25(self, gold_15: float, early_blue: float, early_red: float, bracket: str | None = None) -> Expect:
        return self._expect("gold_25", self.expectations.gold_25_mean(gold_15, early_blue, early_red), None, bracket)

    def expected_gold(self, blue: Mapping[str, int], red: Mapping[str, int], bracket: str | None = None) -> float:
        return self.expectations.gold_mean(blue, red)

    def expected_state(self, blue: Mapping[str, int], red: Mapping[str, int], early_blue: float, early_red: float,
                       bracket: str | None = None) -> dict[str, float]:
        """Blue minus red at 15:00, if everything went as this draft promised."""
        ex = self.expectations
        return {"gold": ex.gold_mean(blue, red),
                **{n: ex.objective_mean(n, blue["jungle"], red["jungle"], early_blue, early_red) for n in OBJECTIVES}}

    def p15(self, p0_logodds: float, state: Mapping[str, float], early_blue: float, early_red: float,
            blue: Mapping[str, int], red: Mapping[str, int], bracket: str | None = None) -> float:
        """Blue's chance given the draft and the state at 15:00 (blue minus red: gold, towers, plates, dragons, grubs, herald)."""
        return self.state.p15(p0_logodds, _row_from_state(self.expectations, state, blue, red, early_blue, early_red))

    # ---------- stored as JSON ----------
    def to_dict(self) -> dict:
        ex = self.expectations
        return {
            "window": self.window, "draft_scale": self.draft_scale, "state": self.state.coefs.tolist(), "report": self.report,
            "bracket_scale": self.bracket_scale, "sds": {k: v.to_dict() for k, v in self.sds.items()},
            "lane": {t: {r: m.to_dict() for r, m in by.items()} for t, by in ex.lane.items()},
            "bot": {t: v.tolist() for t, v in ex.bot.items()}, "jungle_cs": ex.jungle_cs.to_dict(), "kp": ex.kp.to_dict(),
            "vision": ex.vision.to_dict(), "solo": {r: m.to_dict() for r, m in ex.solo.items()},
            "ganked": {r: m.to_dict() for r, m in ex.ganked.items()},
            "objectives": {n: {**o, "jungler": {str(c): v for c, v in o["jungler"].items()}} for n, o in ex.objectives.items()},
            "gold_25": ex.gold_25.tolist(),
        }

    @classmethod
    def from_dict(cls, d: Mapping, labels: Labels) -> ExecutionModel:
        lane = {t: {r: LaneTarget.from_dict(m, labels.features(r, m["champs"])) for r, m in by.items()}
                for t, by in d["lane"].items()}
        ex = Expectations(
            lane, {t: np.array(v) for t, v in d["bot"].items()}, ChampMeans.from_dict(d["jungle_cs"]), ChampMeans.from_dict(d["kp"]),
            ChampMeans.from_dict(d["vision"]), {r: PoissonMatchups.from_dict(m) for r, m in d["solo"].items()},
            {r: PoissonMatchups.from_dict(m) for r, m in d["ganked"].items()},
            {n: {**o, "jungler": {int(c): v for c, v in o["jungler"].items()}} for n, o in d["objectives"].items()},
            np.array(d["gold_25"]))
        return cls(d["window"], ex, {k: SdTable.from_dict(v) for k, v in d["sds"].items()}, d["bracket_scale"], d["draft_scale"],
                   StateModel(np.array(d["state"])), d["report"])


def save_execution_model(db: Engine, model: ExecutionModel) -> None:
    with Session(db) as s:
        row = s.get(ExecutionFit, model.window) or ExecutionFit(window=model.window, body="")
        row.body = json.dumps(model.to_dict())
        s.add(row)
        s.commit()


def load_execution_model(db: Engine) -> ExecutionModel | None:
    with Session(db) as s:
        rows = s.exec(select(ExecutionFit)).all()
    if not rows:
        return None
    from draftassist.data.synergy import latest_window, pick_fit

    latest = pick_fit(rows, latest_window(db))
    return ExecutionModel.from_dict(json.loads(latest.body), Labels(*_labels(db)))


# ---------- the draft's view of crawled games ----------
def _draft_function(db: Engine, games: list[Game], window: str) -> Callable[[str, bool], float]:
    """Blue's draft log-odds per match: the engine with both teams in that match's tier, the game itself taken out of the
    synergy records it would otherwise be predicted from. Cached per match and window."""
    from dataclasses import replace

    from draftassist.api.main import with_synergy_v2
    from draftassist.data.provider import load_engine_inputs
    from draftassist.data.settings_store import engine_config
    from draftassist.engine.engine import DraftEngine
    from draftassist.engine.model import OurTeam

    with Session(db) as s:
        cached = {r.match_id: r.value for r in s.exec(select(MatchDraftLogOdds).where(MatchDraftLogOdds.window == window)).all()}
    inputs = load_engine_inputs(db)
    data = with_synergy_v2(db, inputs)
    cfg = engine_config(db)
    engines: dict[str, DraftEngine] = {}
    with db.connect() as conn:
        weights = crawl_weights(conn, window)
    by_id = {g.match_id: g for g in games}
    fresh: dict[str, float] = {}

    def value(match_id: str, won: bool) -> float:
        if match_id in cached:
            return cached[match_id]
        g = by_id[match_id]
        engine = engines.get(g.bracket)
        if engine is None:
            team = OurTeam({}, dict.fromkeys(ROLES, g.bracket))
            engine = engines[g.bracket] = DraftEngine(data, team, replace(cfg, enemy_bracket=g.bracket))
        raw = engine.draft_logodds(g.blue, g.red)
        ev = data.synergy_v2
        leak = 0.0
        if ev is not None:
            w = weights.get(g.patch, 0.0)
            leak = _stat_leak(ev, g.blue, g.won, w) - _stat_leak(ev, g.red, not g.won, w)
        fresh[match_id] = cached[match_id] = raw - leak
        return cached[match_id]

    def save() -> None:
        with Session(db) as s:
            for match_id, v in fresh.items():
                s.merge(MatchDraftLogOdds(match_id=match_id, window=window, value=v))
            s.commit()
        fresh.clear()

    value.save = save
    return value


def _stat_leak(ev, comp: Mapping[str, int], won: bool, weight: float) -> float:
    """How much this game's own result is in the synergy this comp is scored with."""
    from itertools import combinations

    total = 0.0
    for ra, rb in combinations(ROLES, 2):
        pair = ev.pair(ra, comp[ra], rb, comp[rb])
        score = ev.stat.pair(pair.role_a, pair.a, pair.role_b, pair.b)
        if score.games <= 0 or pair.stat == 0:
            continue
        share = pair.stat_part / pair.stat
        total += share * pair_stat_leak(score, won, weight, score.games, ev.stat.strength(pair.a, pair.role_a),
                                        ev.stat.strength(pair.b, pair.role_b))
    return total


# ---------- fitting ----------
def _sd_tables(residuals: list[tuple[str, str, float]], brackets: list[str]) -> tuple[dict, dict]:
    grouped: dict[str, tuple[list, list, list]] = {}
    for (stat, key, value), bracket in zip(residuals, brackets):
        keys, values, where = grouped.setdefault(stat, ([], [], []))
        keys.append(key)
        values.append(value)
        where.append(bracket)
    sds, scales = {}, {}
    for stat, (keys, values, where) in grouped.items():
        table = SdTable.fit(keys, values)
        sds[stat] = table
        y, at = np.array(values, dtype=float), np.array(where)
        scales[stat] = {}
        for b in sorted(set(where)):
            chosen = y[at == b]
            ratio = float(np.sqrt(np.mean(chosen ** 2))) / table.pool if table.pool_var else 1.0
            scales[stat][b] = (len(chosen) * ratio + 200.0) / (len(chosen) + 200.0)
    return sds, scales


def _calibration(p: np.ndarray, won: np.ndarray) -> dict:
    return {"brier": brier(p, won), "table": reliability_table(p, won)}


def fit_execution(db: Engine, window: str | None = None, draft: Callable[[str, bool], float] | None = None,
                  priors_path=PRIORS_FILE) -> ExecutionModel:
    """Fit, store and return everything. `draft`: blue's draft log-odds for a match (default: the engine's)."""
    window = window or current_patch(db)
    profiles = load_profiles(db, window)
    games = load_games(db, profiles)
    if not games:
        raise ValueError("no crawled matches with full timelines yet: run data.crawl --timelines-refetch")
    labels = Labels(*_labels(db))
    priors = load_archetype_priors(priors_path)
    role_champs = {r: sorted({c for g in games for c in (g.blue[r], g.red[r])}) for r in ROLES}

    draft_of = draft or _draft_function(db, games, window)
    for g in games:
        g.draft = draft_of(g.match_id, g.won)
    if hasattr(draft_of, "save"):
        draft_of.save()

    # Every game's expected values from the folds it isn't in
    residuals, brackets, rows = [], [], {}
    for fold in range(FOLDS):
        train = [g for g in games if g.fold != fold]
        test = [g for g in games if g.fold == fold]
        if not train or not test:
            continue
        ex = _fit_expectations(train, labels, role_champs, priors)
        for g in test:
            found = _residuals(ex, g)
            residuals += found
            brackets += [g.bracket] * len(found)
            rows[g.match_id] = _state_residual(ex, g)
    sds, scales = _sd_tables(residuals, brackets)

    used = [g for g in games if g.match_id in rows]
    x = np.array([rows[g.match_id] for g in used])
    d = np.array([g.draft for g in used])
    won = np.array([g.won for g in used], dtype=bool)
    held = np.array([g.fold == HELD_OUT for g in used], dtype=bool)
    at_b = np.array([g.bracket for g in used])

    # The held-out fold decides whether this is worth anything; only then is everything fitted on all of it
    scale_train = fit_draft_scale(d[~held], won[~held])
    state_train = fit_state(x[~held], scale_train * d[~held], won[~held])
    p_draft = sigmoid_array(scale_train * d[held])
    p_state = sigmoid_array(scale_train * d[held] + x[held] @ state_train.coefs)
    per_bracket = {}
    for b in sorted(set(at_b[held])):
        here = at_b[held] == b
        per_bracket[b] = {"matches": int(here.sum()), "state_brier": brier(p_state[here], won[held][here]),
                          "draft_brier": brier(p_draft[here], won[held][here])}
    state_brier, draft_brier = brier(p_state, won[held]), brier(p_draft, won[held])
    report = {
        "matches": len(used), "beats_draft": bool(state_brier < draft_brier),
        "held_out": {"matches": int(held.sum()), "state_brier": state_brier, "draft_brier": draft_brier,
                     "state_table": reliability_table(p_state, won[held]), "draft_table": reliability_table(p_draft, won[held])},
        "per_bracket": per_bracket,
    }
    scale = fit_draft_scale(d, won)
    state = fit_state(x, scale * d, won)
    report["draft_scale"] = scale
    report["coefs"] = dict(zip(STATE_FEATURES, state.coefs.tolist()))
    model = ExecutionModel(window, _fit_expectations(games, labels, role_champs, priors), sds, scales, scale, state, report)
    save_execution_model(db, model)
    return model


def sigmoid_array(x: np.ndarray) -> np.ndarray:
    return np.array([sigmoid(float(v)) for v in x])


# ---------- the report ----------
def format_execution_report(db: Engine) -> str:
    """Part of the coverage report: is the state model calibrated, and does it beat the draft alone?"""
    model = load_execution_model(db)
    if model is None:
        return "State model: not fitted yet (run `uv run python -m draftassist.data.execution_model`)."
    r = model.report
    held = r["held_out"]
    verdict = "beats draft-only" if r["beats_draft"] else "does NOT beat draft-only: stop and look before using it"
    lines = [f"State model, window {model.window}: {r['matches']:,} matches, {held['matches']:,} held out; {verdict}",
             f"  Brier on held-out games: state {held['state_brier']:.4f}, draft only {held['draft_brier']:.4f}, coin flip 0.2500",
             f"  Draft log-odds scale: {r['draft_scale']:.2f} (1 = the draft model's numbers are already right)",
             "  What 15 minutes are worth, log-odds per unit against expectation: "
             + ", ".join(f"{k} {v:+.3f}" for k, v in r["coefs"].items()),
             "  Calibration on held-out games: predicted bin, games, predicted, won"]
    for row in held["state_table"]:
        if row["n"]:
            lines.append(f"    {row['bin']:>8} {row['n']:>6}  {row['predicted']:.1%}  {row['win_rate']:.1%}")
        else:
            lines.append(f"    {row['bin']:>8} {0:>6}")
    lines.append("  Per tier, held out: state vs draft-only Brier")
    for b, v in r["per_bracket"].items():
        lines.append(f"    {b:<9} {v['matches']:>5} games  {v['state_brier']:.4f}  {v['draft_brier']:.4f}")
    return "\n".join(lines)


def main() -> int:
    from draftassist.config import load_settings
    from draftassist.db.session import init_db

    db = init_db(load_settings().db_path)
    fit_execution(db)
    print(format_execution_report(db))

    from draftassist.learning.execution import refresh_executions

    print(f"{refresh_executions(db)} logged games attributed")  # a new fit changes what every game is compared with
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
