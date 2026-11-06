"""Stage 2 of docs/draft-vs-execution.md: what each stat was expected to be, and what a game state at 15 minutes is worth."""
from __future__ import annotations

import math
import random
import zlib

import numpy as np
import pytest
from sqlmodel import Session

from draftassist.data.execution_model import (
    ExecutionModel,
    fit_execution,
    format_execution_report,
    load_execution_model,
)
from draftassist.db.models import (
    Champion,
    CrawledMatch,
    MatchParticipant,
    MatchTeamState,
)
from draftassist.engine.execution import (
    STATE_FEATURES,
    ChampMeans,
    LaneTarget,
    PoissonMatchups,
    SdTable,
    StateModel,
    brier,
    early_of_leader,
    fit_draft_scale,
    fit_ridge,
    fit_state,
    pair_stat_leak,
    reliability_table,
    shrunk_sd,
    state_row,
)
from draftassist.engine.matchup_model import RoleFeatures
from draftassist.engine.mathx import logit, sigmoid
from draftassist.engine.model import ROLES
from draftassist.engine.synergy import pair_score
from draftassist.engine.traits import KitTraits


# ---------- test 1: planted effects are recovered ----------
def _games(n=30_000, gold=0.5, tower=0.15, timing=0.2, scale=1.0, seed=3):
    rng = np.random.default_rng(seed)
    draft = rng.normal(0, 0.4, n)
    g = rng.normal(0, 2.5, n)  # thousands of gold from expected
    towers = rng.normal(0, 1.0, n)
    early_b, early_r = rng.normal(0, 0.5, n), rng.normal(0, 0.5, n)
    rows = np.array([state_row(gold=g[i], towers=towers[i], plates=0, dragons=0, grubs=0, herald=0,
                               early_blue=early_b[i], early_red=early_r[i]) for i in range(n)])
    eta = scale * draft + gold * g + tower * towers + timing * rows[:, STATE_FEATURES.index("timing_gold")]
    return draft, rows, rng.random(n) < 1 / (1 + np.exp(-eta))


def test_the_state_model_recovers_a_planted_gold_coefficient_and_timing_interaction():
    draft, rows, won = _games()
    model = fit_state(rows, draft, won)
    at = {name: i for i, name in enumerate(STATE_FEATURES)}
    assert model.coefs[at["gold"]] == pytest.approx(0.5, abs=0.03)
    assert model.coefs[at["towers"]] == pytest.approx(0.15, abs=0.03)
    assert model.coefs[at["timing_gold"]] == pytest.approx(0.2, abs=0.05)
    assert abs(model.coefs[at["dragons"]]) < 1e-6  # an all-zero column stays at nothing


def test_the_draft_scale_is_recovered():
    draft, _, won = _games(gold=0.0, tower=0.0, timing=0.0, scale=0.4)
    assert fit_draft_scale(draft, won) == pytest.approx(0.4, abs=0.04)


def test_a_lead_is_worth_more_to_the_earlier_team_on_the_team_that_has_it():
    """Blue ahead: blue's earliness counts. Red ahead: red's does. Swapping the teams only flips the sign."""
    assert early_of_leader(2.0, 0.5, -0.5) == 0.5
    assert early_of_leader(-2.0, 0.5, -0.5) == -0.5
    blue = state_row(gold=2, towers=0, plates=0, dragons=0, grubs=0, herald=0, early_blue=0.5, early_red=-0.5)
    red = state_row(gold=-2, towers=0, plates=0, dragons=0, grubs=0, herald=0, early_blue=-0.5, early_red=0.5)
    assert red[STATE_FEATURES.index("timing_gold")] == -blue[STATE_FEATURES.index("timing_gold")] == -1.0


def _feats(words=("a", "b"), champs=(1, 2, 3)):
    class Label:
        def __init__(self, primary):
            self.primary, self.secondary, self.reviewed = primary, None, True

    labels = {c: Label(words[i % len(words)]) for i, c in enumerate(champs)}
    return RoleFeatures("top", words, list(champs), labels, KitTraits({}), interactions=())


def _lane_rows(rng, means, n=4000, noise=300.0):
    rows = []
    pairs = list(means)
    for _ in range(n):
        a, b = pairs[rng.randrange(len(pairs))]
        y = means[(a, b)] + rng.gauss(0, noise)
        rows.append((a, b, y) if rng.random() < 0.5 else (b, a, -y))
    return rows


def test_a_lane_target_recovers_a_planted_matchup_and_is_antisymmetric():
    rng = random.Random(1)
    rows = _lane_rows(rng, {(1, 2): 400.0, (1, 3): 300.0, (2, 3): -100.0})
    target = LaneTarget.fit(_feats(), rows, np.zeros(1), prior_sd=500.0)
    assert target.expected(1, 2) == pytest.approx(400, abs=40)
    assert target.expected(2, 1) == -target.expected(1, 2)
    assert target.expected(1, 1) == 0.0
    assert target.expected(1, 99) == 0.0  # a champion it has never seen: no opinion


def test_a_sparse_pair_leans_on_the_champions_not_on_its_own_games():
    rows = [(1, 3, 300.0)] * 3000 + [(2, 3, 300.0)] * 3000 + [(1, 2, 5000.0)]  # one wild game
    target = LaneTarget.fit(_feats(), rows, np.zeros(1), prior_sd=500.0)
    assert abs(target.expected(1, 2)) < 1000


def test_champion_means_shrink_and_carry_a_bracket_adjustment():
    keys = [1] * 6000 + [2] * 3
    y = [120.0] * 6000 + [200.0] * 3
    brackets = (["iron"] * 3000 + ["emerald"] * 3000) + ["iron"] * 3
    y = [v - 10 if b == "iron" else v + 10 for v, b in zip(y, brackets)]
    means = ChampMeans.fit(keys, y, brackets, k=50)
    assert means.mean(1) == pytest.approx(120, abs=2)
    assert 120 < means.mean(2) < 135  # three games barely move it
    assert means.mean(1, "emerald") - means.mean(1, "iron") == pytest.approx(20, abs=2)  # a real tier's thousands of games
    assert means.mean(99) == means.overall  # never seen

    few = ChampMeans.fit([1] * 40, [100.0] * 20 + [120.0] * 20, ["iron"] * 20 + ["emerald"] * 20, k=50)
    assert few.mean(1, "emerald") - few.mean(1, "iron") < 5  # forty games barely say anything about a tier


def test_a_poisson_matchup_recovers_who_dies_to_whom():
    rng = np.random.default_rng(5)
    rows = []
    rates = {(1, 3): 1.2, (2, 3): 0.4, (1, 4): 0.5, (2, 4): 0.2}
    for (victim, killer), rate in rates.items():
        rows += [(victim, killer, float(v)) for v in rng.poisson(rate, 3000)]
    model = PoissonMatchups.fit(rows)
    assert model.mean(1, 3) == pytest.approx(1.2, abs=0.08)
    assert model.mean(2, 4) == pytest.approx(0.2, abs=0.05)
    assert model.mean(99, 98) == pytest.approx(model.overall)  # no idea: the role's own rate


def test_ridge_recovers_a_planted_objective_effect():
    rng = np.random.default_rng(2)
    x = rng.normal(size=(5000, 3))
    y = x @ np.array([0.5, -0.2, 0.0]) + rng.normal(0, 0.3, 5000)
    assert fit_ridge(x, y, penalty=1.0) == pytest.approx(np.array([0.5, -0.2, 0.0]), abs=0.03)


# ---------- test 2: at expectation, P15 is P0 ----------
def test_with_every_feature_at_zero_p15_equals_p0():
    draft, rows, won = _games(n=4000)
    model = fit_state(rows, draft, won)
    zero = state_row(gold=0, towers=0, plates=0, dragons=0, grubs=0, herald=0, early_blue=0.7, early_red=-0.3)
    for p0 in (0.1, 0.42, 0.5, 0.77):
        assert model.p15(logit(p0), zero) == pytest.approx(p0, abs=1e-9)


# ---------- test 3: a residual SD for every expectation, shrinking for sparse cells ----------
def test_a_sparse_cell_leans_on_the_pool_and_a_big_one_on_itself():
    assert shrunk_sd(0, 100.0, 400.0, k=50) == pytest.approx(20)
    assert shrunk_sd(100_000, 100.0, 400.0, k=50) == pytest.approx(10, abs=0.01)
    assert 10 < shrunk_sd(50, 100.0, 400.0, k=50) < 20


def test_an_sd_table_reads_cells_and_falls_back_to_the_pool():
    rng = np.random.default_rng(1)
    keys = ["wide"] * 5000 + ["narrow"] * 5000 + ["thin"] * 3
    values = list(rng.normal(0, 50, 5000)) + list(rng.normal(0, 10, 5000)) + [40.0, -40.0, 40.0]
    table = SdTable.fit(keys, values, k=50)
    assert table.sd("wide") == pytest.approx(50, abs=3)
    assert table.sd("narrow") == pytest.approx(10, abs=3)
    assert table.sd("thin") == pytest.approx(table.pool, abs=3)  # three games prove nothing
    assert table.sd("never") == pytest.approx(table.pool)
    assert table.pool > 0


# ---------- leave-one-out on the synergy records ----------
def test_taking_a_game_out_of_a_pair_record_matches_computing_it_without_that_game():
    p_a, p_b = 0.52, 0.49
    with_it = pair_score(60.0, 100.0, p_a, p_b)
    without = pair_score(59.0, 99.0, p_a, p_b)
    leak = pair_stat_leak(with_it, won=True, weight=1.0, games=100.0, p_a=p_a, p_b=p_b)
    assert leak == pytest.approx(with_it.s - without.s, abs=1e-9)
    assert leak > 0  # a win it had already counted

    lost = pair_score(60.0, 100.0, p_a, p_b)
    assert pair_stat_leak(lost, won=False, weight=1.0, games=100.0, p_a=p_a, p_b=p_b) == pytest.approx(
        lost.s - pair_score(60.0, 99.0, p_a, p_b).s, abs=1e-9)


def _engine_and_comps():
    from draftassist.engine.engine import DraftEngine
    from draftassist.engine.model import EngineConfig
    from draftassist.engine.simulate import synthetic_data

    data, team = synthetic_data()
    meta = data.brackets[EngineConfig().enemy_bracket].meta
    ranked = {r: sorted((c for c in meta if r in meta[c]), key=lambda c: -meta[c][r].pick_rate) for r in ROLES}
    used: set[int] = set()
    blue, red = {}, {}
    for role in ROLES:
        free = [c for c in ranked[role] if c not in used]
        blue[role], red[role] = free[0], free[1]
        used |= {free[0], free[1]}
    return DraftEngine(data, team, EngineConfig(alpha=1.5, beta=0.3)), blue, red


def test_draft_log_odds_have_no_home_side_and_no_comfort():
    engine, blue, red = _engine_and_comps()
    assert engine.draft_logodds(blue, red) == pytest.approx(-engine.draft_logodds(red, blue))
    assert engine.draft_logodds(blue, blue) == pytest.approx(0.0, abs=1e-9)
    roles_b, roles_r = {c: r for r, c in blue.items()}, {c: r for r, c in red.items()}
    terms = engine._terms(list(roles_b), roles_b, list(roles_r), roles_r)
    assert terms.comfort != 0  # there is comfort to leave out
    reverse = engine._terms(list(roles_r), roles_r, list(roles_b), roles_b)
    assert engine.draft_logodds(blue, red) == pytest.approx(
        ((terms.total - terms.comfort) - (reverse.total - reverse.comfort)) / 2)


def test_a_comps_stat_leak_is_weighted_as_the_comp_was_scored():
    from draftassist.data.execution_model import _stat_leak
    from draftassist.engine.synergy import SynergyTable
    from draftassist.engine.synergy_v2 import SynergyEvaluator, SynergyWeights

    class NoStyle:
        def style(self, traits, champs):
            return 0.0

        def explain(self, traits, champs):
            return ()

    comp = dict(zip(ROLES, (1, 2, 3, 4, 5)))
    score = pair_score(60.0, 100.0, 0.5, 0.5)
    keyed = {(("jungle", 2), ("top", 1)): score, (("mid", 3), ("top", 1)): score}  # one core pair, one not
    table = SynergyTable("16.21", "all", keyed, {})
    weights = SynergyWeights()
    ev = SynergyEvaluator(table, None, NoStyle(), None, weights)
    leak = pair_stat_leak(score, True, 1.0, 100.0, 0.5, 0.5)
    conf = 100 / (100 + weights.conf_k)
    assert _stat_leak(ev, comp, True, 1.0) == pytest.approx(leak * weights.stat * (conf + 1), rel=0.02)
    assert _stat_leak(ev, comp, True, 1.0) > 0 > _stat_leak(ev, comp, False, 1.0)


# ---------- calibration ----------
def test_the_reliability_table_and_brier_score():
    p = np.array([0.05, 0.05, 0.55, 0.55, 0.95, 0.95])
    won = np.array([0, 0, 1, 0, 1, 1], dtype=bool)
    rows = reliability_table(p, won)
    assert [r["n"] for r in rows if r["n"]] == [2, 2, 2]
    assert [r["win_rate"] for r in rows if r["n"]] == [0.0, 0.5, 1.0]
    assert len(rows) == 10
    assert brier(np.array([1.0, 0.0]), np.array([True, False])) == 0.0
    assert brier(np.array([0.5, 0.5]), np.array([True, False])) == 0.25


# ---------- end to end on a small planted crawl ----------
POSITION_CHAMPS = {"top": (1, 2), "jungle": (3, 4), "mid": (5, 6), "adc": (7, 8), "support": (9, 10)}


def _crawl(engine, n=2500, seed=9):
    """Champion 1 wins lane by 400 gold against 2. A gold lead at 15 decides games: 0.6 log-odds per 1,000."""
    rng = random.Random(seed)
    with Session(engine) as s:
        for c in range(1, 11):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.22.1", tags=["Fighter"]))
        for i in range(n):
            match_id = f"EUW1_{i}"
            flip = rng.random() < 0.5
            lanes = {r: (cs[1], cs[0]) if flip else cs for r, cs in POSITION_CHAMPS.items()}
            leads = {r: rng.gauss(0, 500) for r in ROLES}
            leads["top"] += -400 if flip else 400
            team_gap = sum(leads.values())
            won = rng.random() < sigmoid(0.6 * (team_gap - (-400 if flip else 400)) / 1000)
            bracket = rng.choice(("iron", "gold", "emerald"))
            s.add(CrawledMatch(match_id=match_id, patch="16.21", bracket=bracket, queue_id=420, game_start_ms=i,
                               game_duration_s=1800, timeline_done=True, timeline_version=2))
            s.flush()
            for role in ROLES:
                blue, red = lanes[role]
                for team, champ, sign, win in ((100, blue, 1, won), (200, red, -1, not won)):
                    lead = sign * leads[role]
                    jungle = role == "jungle"
                    s.add(MatchParticipant(
                        match_id=match_id, champ_id=champ, team_id=team, role=role, win=win, lane_gold_15=lead,
                        lane_xp_15=lead / 2, lane_cs_14=lead / 20, cs_15=100.0, jungle_cs_10=60.0 if jungle else 0.0,
                        jungle_cs_15=90.0 + (10.0 if champ == 3 else 0.0) if jungle else 0.0, level_15=10.0,
                        deaths_solo_14=float(rng.random() < 0.3), deaths_ganked_14=float(rng.random() < 0.2),
                        deaths_other_14=0.0, kp_15=0.5, plates_14=0.0, wards_placed_15=8.0 if role == "support" else 2.0,
                        wards_killed_15=1.0))
            for team, sign in ((100, 1), (200, -1)):
                s.add(MatchTeamState(match_id=match_id, team_id=team, gold_15=25000 + max(sign * team_gap, 0),
                                     kills_15=5.0, towers_15=0.0, plates_15=3.0, dragons_15=1.0, grubs_15=1.0, herald_15=0.0,
                                     gold_25=40000 + max(sign * team_gap, 0) * 1.5, dragons_after_15=1.0, barons=0.0,
                                     towers_after_15=1.0, deaths_while_ahead=0.0))
        s.commit()


@pytest.fixture
def fitted(engine):
    _crawl(engine)
    return engine, fit_execution(engine, draft=lambda match, won: 0.0)


def test_the_fit_finds_the_planted_lane_and_beats_draft_only(fitted):
    _, model = fitted
    assert model.lane("gold", "top", 1, 2, "gold").mean == pytest.approx(400, abs=60)
    assert model.lane("gold", "top", 2, 1, "gold").mean == -model.lane("gold", "top", 1, 2, "gold").mean
    assert model.lane("xp", "top", 1, 2, "gold").mean == pytest.approx(200, abs=40)
    assert model.lane("cs", "top", 1, 2, "gold").mean == pytest.approx(20, abs=4)
    report = model.report
    assert report["matches"] == 2500
    assert report["held_out"]["state_brier"] < report["held_out"]["draft_brier"]
    assert report["beats_draft"] is True
    assert model.state.coefs[STATE_FEATURES.index("gold")] == pytest.approx(0.6, abs=0.1)


def test_every_expectation_has_a_positive_sd(fitted):
    _, model = fitted
    checks = [model.lane(t, r, *POSITION_CHAMPS[r], "gold") for t in ("gold", "xp", "cs") for r in ("top", "jungle", "mid")]
    checks += [model.bot(t, 7, 9, 8, 10, "gold") for t in ("gold", "xp")]
    checks += [model.jungle_cs(3, "gold"), model.kp(3, "gold"), model.vision(9, "gold")]
    checks += [model.deaths_solo("top", 1, 2, "gold"), model.deaths_ganked("top", 1, 4, "gold")]
    checks += [model.objective(o, 3, 4, 0.1, -0.1, "gold") for o in ("dragons", "grubs", "herald", "towers", "plates")]
    checks.append(model.gold_25(1000.0, 0.1, -0.1))
    assert all(c.sd > 0 and math.isfinite(c.mean) for c in checks)


def test_the_jungle_mean_reads_the_champion(fitted):
    _, model = fitted
    assert model.jungle_cs(3, "gold").mean == pytest.approx(100, abs=1.5)
    assert model.jungle_cs(4, "gold").mean == pytest.approx(90, abs=1.5)


def test_the_expected_team_gold_is_the_sum_of_its_lanes(fitted):
    _, model = fitted
    blue = {"top": 1, "jungle": 3, "mid": 5, "adc": 7, "support": 9}
    red = {"top": 2, "jungle": 4, "mid": 6, "adc": 8, "support": 10}
    total = sum(model.lane("gold", r, blue[r], red[r], "gold").mean for r in ("top", "jungle", "mid"))
    total += model.bot("gold", blue["adc"], blue["support"], red["adc"], red["support"], "gold").mean
    assert model.expected_gold(blue, red, "gold") == pytest.approx(total)


def test_a_game_at_every_expectation_has_p15_equal_to_p0(fitted):
    _, model = fitted
    blue = {"top": 1, "jungle": 3, "mid": 5, "adc": 7, "support": 9}
    red = {"top": 2, "jungle": 4, "mid": 6, "adc": 8, "support": 10}
    expected = model.expected_state(blue, red, 0.1, -0.1, "gold")
    assert model.p15(logit(0.56), expected, 0.1, -0.1, blue, red, "gold") == pytest.approx(0.56, abs=1e-9)


def test_being_ahead_of_expectation_raises_p15_and_the_expected_lead_does_not(fitted):
    _, model = fitted
    blue = {"top": 1, "jungle": 3, "mid": 5, "adc": 7, "support": 9}
    red = {"top": 2, "jungle": 4, "mid": 6, "adc": 8, "support": 10}
    expected = model.expected_state(blue, red, 0.0, 0.0, "gold")
    assert expected["gold"] > 0  # champion 1 is expected to lead
    ahead = {**expected, "gold": expected["gold"] + 2000}
    behind = {**expected, "gold": expected["gold"] - 2000}
    assert model.p15(0.0, ahead, 0.0, 0.0, blue, red, "gold") > 0.5 > model.p15(0.0, behind, 0.0, 0.0, blue, red, "gold")


def test_the_model_survives_being_stored(fitted):
    engine, model = fitted
    loaded = load_execution_model(engine)
    assert isinstance(loaded, ExecutionModel)
    assert loaded.lane("gold", "top", 1, 2, "gold").mean == pytest.approx(model.lane("gold", "top", 1, 2, "gold").mean)
    assert loaded.lane("gold", "top", 1, 2, "gold").sd == pytest.approx(model.lane("gold", "top", 1, 2, "gold").sd)
    assert loaded.deaths_solo("top", 1, 2, "gold").mean == pytest.approx(model.deaths_solo("top", 1, 2, "gold").mean)
    assert loaded.objective("dragons", 3, 4, 0.1, -0.1, "gold").mean == pytest.approx(
        model.objective("dragons", 3, 4, 0.1, -0.1, "gold").mean)
    assert list(loaded.state.coefs) == pytest.approx(list(model.state.coefs))
    assert loaded.report == model.report


def test_nothing_stored_means_no_model(engine):
    assert load_execution_model(engine) is None


def test_held_out_matches_never_train_the_state_model(engine):
    _crawl(engine, n=600)
    seen = []

    def draft(match, won):
        seen.append(match)
        return 0.0

    model = fit_execution(engine, draft=draft)
    held = [m for m in seen if zlib.crc32(m.encode()) % 5 == 0]
    assert held and model.report["held_out"]["matches"] == len(held)


# ---------- test 4: the coverage report ----------
def test_the_report_prints_calibration_and_the_brier_comparison(fitted):
    engine, _ = fitted
    text = format_execution_report(engine)
    assert "State model" in text and "Brier" in text and "beats draft-only" in text
    assert "0-10%" in text and "90-100%" in text
    assert "iron" in text and "emerald" in text  # calibration checked per bracket


def test_the_report_says_so_when_the_model_does_not_beat_draft_only(fitted):
    engine, model = fitted
    from draftassist.data.execution_model import save_execution_model

    model.report["beats_draft"] = False
    save_execution_model(engine, model)
    assert "does NOT beat draft-only: stop" in format_execution_report(engine)


def test_the_report_without_a_model_says_how_to_make_one(engine):
    assert "not fitted" in format_execution_report(engine)
