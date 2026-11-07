"""Stage 3 of docs/draft-vs-execution.md: one game split into draft, early game and conversion."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select
from test_execution_model import _crawl

from draftassist.data.execution_model import fit_execution
from draftassist.db.models import (
    Draft,
    DraftAction,
    GameExecution,
    GameExecutionRole,
    MatchTeamState,
    OwnGameParticipant,
)
from draftassist.engine.engine import DraftEngine
from draftassist.engine.execution import Expect
from draftassist.engine.mathx import logit, sigmoid
from draftassist.engine.simulate import synthetic_data
from draftassist.learning.execution import (
    RULES,
    DraftScorer,
    GameFacts,
    attribute,
    label,
    refresh_executions,
    verdict,
)

BLUE = {"top": 1, "jungle": 3, "mid": 5, "adc": 7, "support": 9}
RED = {"top": 2, "jungle": 4, "mid": 6, "adc": 8, "support": 10}
SD = 100.0


class Flat:
    """The model's interface, with round numbers. Leads are from the first champion's view and flip with the pair; the
    state model is gold + dragons, 0.4 log-odds per 1,000 gold and 0.3 per dragon."""
    LANE = {"top": 300.0, "jungle": -100.0, "mid": 200.0, "adc": 50.0, "support": 25.0}
    BOT = 150.0
    B_GOLD, B_DRAGON = 0.4, 0.3

    UNITS = {"gold": 1.0, "xp": 0.5, "cs": 0.05}  # as player() below

    def lane(self, target, role, a, b, bracket=None):
        return Expect(self.UNITS[target] * self.LANE[role] * (1 if a < b else -1), SD)

    def bot(self, target, adc, sup, e_adc, e_sup, bracket=None):
        return Expect(self.UNITS[target] * self.BOT * (1 if adc < e_adc else -1), SD)

    def jungle_cs(self, champ, bracket=None):
        return Expect(90.0, 10.0)

    def kp(self, champ, bracket=None):
        return Expect(0.5, 0.2)

    def vision(self, champ, bracket=None):
        return Expect(1.0, 0.25)

    def deaths_solo(self, role, champ, opponent, bracket=None):
        return Expect(0.5, 0.5)

    def deaths_ganked(self, role, champ, enemy_jungler, bracket=None):
        return Expect(0.25, 0.5)

    def objective(self, name, j_blue, j_red, early_blue, early_red, bracket=None):
        return Expect({"dragons": 0.5, "grubs": 0.25}.get(name, 0.0), 1.0)

    def gold_25(self, gold_15, early_blue, early_red, bracket=None):
        return Expect(1.5 * gold_15, 1000.0)

    def expected_gold(self, blue, red, bracket=None):
        return sum(self.lane("gold", r, blue[r], red[r]).mean for r in ("top", "jungle", "mid")) + self.BOT

    def p15(self, p0_logodds, state, early_blue, early_red, blue, red, bracket=None):
        gold = (state["gold"] - self.expected_gold(blue, red)) / 1000
        return sigmoid(p0_logodds + self.B_GOLD * gold + self.B_DRAGON * (state["dragons"] - 0.5))


def player(lead, role=None):
    return {"lane_gold_15": lead, "lane_xp_15": lead / 2, "lane_cs_14": lead / 20, "jungle_cs_15": 90.0, "kp_15": 0.5,
            "deaths_solo_14": 0.5, "deaths_ganked_14": 0.25, "plates_14": 0.0, "wards_placed_15": 10.0, "wards_killed_15": 5.0}


def team(gold, dragons=0.0, grubs=0.0, **more):
    return {"gold_15": gold, "towers_15": 0.0, "plates_15": 0.0, "dragons_15": dragons, "grubs_15": grubs, "herald_15": 0.0,
            "gold_25": None, "dragons_after_15": 0.0, "barons": 0.0, "towers_after_15": 0.0, "deaths_while_ahead": 0.0, **more}


def on_plan(side="blue", won=True, moved=None, dragons=0.5, grubs=0.25, red_extra=None, blue_extra=None):
    """A game where every stat is what the model expected, except blue's leads in `moved` (role -> gold from expected)."""
    moved = moved or {}
    m = Flat()
    leads = {r: m.lane("gold", r, BLUE[r], RED[r]).mean + moved.get(r, 0.0) for r in ("top", "jungle", "mid")}
    leads["adc"] = m.BOT / 2 + moved.get("adc", 0.0)
    leads["support"] = m.BOT / 2 + moved.get("support", 0.0)
    players = {}
    for role in leads:
        players[BLUE[role]] = player(leads[role])
        players[RED[role]] = player(-leads[role])
    return GameFacts(side=side, won=won, blue=BLUE, red=RED, players=players,
                     blue_state=team(25_000 + sum(leads.values()), dragons=dragons, grubs=grubs, **(blue_extra or {})),
                     red_state=team(25_000, **(red_extra or {})), early_blue=0.0, early_red=0.0,
                     brackets=dict.fromkeys(BLUE, "gold"), team_bracket="gold")


# ---------- test 1: everything adds up ----------
@pytest.mark.parametrize("side,won", [("blue", True), ("blue", False), ("red", True), ("red", False)])
def test_the_parts_sum_to_the_result(side, won):
    a = attribute(Flat(), on_plan(side, won, moved={"top": 1200, "mid": -500, "adc": 300, "support": -100}, dragons=2.0), 0.54)
    assert a.draft + a.early + a.conversion == pytest.approx((1.0 if won else 0.0) - 0.5, abs=1e-9)
    shares = sum(a.roles[r].share for r in ("top", "jungle", "mid", "bot", "objectives"))
    assert shares == pytest.approx(a.early, abs=1e-9)


def test_adc_and_support_add_up_to_the_bot_chip():
    a = attribute(Flat(), on_plan(moved={"adc": 900, "support": -300}), 0.5)
    assert a.roles["adc"].share + a.roles["support"].share == pytest.approx(a.roles["bot"].share, abs=1e-12)
    assert a.roles["adc"].share > 0 > a.roles["support"].share


def test_the_definitions():
    a = attribute(Flat(), on_plan(moved={"top": 1000}), 0.56)
    assert a.p0 == 0.56
    assert a.p15 == pytest.approx(sigmoid(logit(0.56) + 0.4))
    assert a.draft == pytest.approx(0.06)
    assert a.early == pytest.approx(a.p15 - 0.56)
    assert a.conversion == pytest.approx(1 - a.p15)


def test_a_role_share_is_what_p15_loses_without_that_roles_lead():
    a = attribute(Flat(), on_plan(moved={"top": 1000, "mid": 500}), 0.5)
    assert a.roles["top"].share == pytest.approx(sigmoid(0.4 * 1.5) - sigmoid(0.4 * 0.5))
    assert a.roles["mid"].share == pytest.approx(sigmoid(0.4 * 1.5) - sigmoid(0.4 * 1.0))


def test_objectives_above_expectation_land_in_the_remainder():
    a = attribute(Flat(), on_plan(dragons=2.5), 0.5)
    assert a.early > 0
    assert a.roles["objectives"].share == pytest.approx(a.early)
    assert all(a.roles[r].share == 0 for r in ("top", "jungle", "mid", "bot"))


def test_red_reads_the_same_game_the_other_way_round():
    blue = attribute(Flat(), on_plan("blue", True, moved={"top": 1000}), 0.5)
    red = attribute(Flat(), on_plan("red", False, moved={"top": 1000}), 0.5)
    assert red.p15 == pytest.approx(1 - blue.p15)
    assert red.early == pytest.approx(-blue.early)
    assert red.roles["top"].share == pytest.approx(-blue.roles["top"].share)
    assert red.roles["top"].headline.label == "well behind"


# ---------- test 2: a game exactly on plan ----------
def all_stats(a):
    return [s for r in a.roles.values() for s in (r.headline, *r.stats)]


@pytest.mark.parametrize("side", ["blue", "red"])
def test_on_plan_early_is_nothing_and_every_chip_is_as_expected(side):
    a = attribute(Flat(), on_plan(side), 0.5)
    assert a.early == pytest.approx(0, abs=1e-9)
    assert {s.label for s in all_stats(a)} == {"as expected"}
    assert {r.label for r in a.roles.values()} == {"as expected"}


@pytest.mark.parametrize("p0,won,expected", [(0.40, False, "Draft-limited"), (0.60, True, "Won on the draft"),
                                             (0.50, True, "Inconclusive"), (0.50, False, "Inconclusive")])
def test_on_plan_the_verdict_comes_from_the_draft_and_the_result(p0, won, expected):
    assert attribute(Flat(), on_plan(won=won), p0).verdict == expected


# ---------- test 3: each verdict rule ----------
@pytest.mark.parametrize("p0,p15,won,expected", [
    (0.50, 0.60, False, "Lost from ahead"),
    (0.50, 0.40, True, "Comeback"),
    (0.45, 0.45, False, "Draft-limited"),
    (0.55, 0.55, True, "Won on the draft"),
    (0.54, 0.44, False, "Lost in the early game"),
    (0.46, 0.56, True, "Won the early game"),
    (0.50, 0.52, True, "Inconclusive"),
    (0.50, 0.44, False, "Inconclusive"),  # 6pp behind isn't a lost early game
    (0.46, 0.56, False, "Won the early game"),  # as the doc has it: this rule doesn't read the result
    (0.54, 0.44, True, "Lost in the early game"),
])
def test_each_verdict_rule(p0, p15, won, expected):
    assert verdict(p0, p15, p15 - p0, won, RULES) == expected


def test_the_rules_run_in_order():
    assert verdict(0.40, 0.65, 0.25, False, RULES) == "Lost from ahead"
    assert verdict(0.60, 0.35, -0.25, True, RULES) == "Comeback"
    assert verdict(0.40, 0.35, -0.05, False, RULES) == "Draft-limited"  # also behind, but early moved it little


def test_a_stronger_threshold_in_config_changes_the_verdict():
    from dataclasses import replace

    assert verdict(0.50, 0.60, 0.10, False, replace(RULES, from_ahead=0.70)) != "Lost from ahead"


# ---------- labels ----------
@pytest.mark.parametrize("z,expected", [(0, "as expected"), (0.99, "as expected"), (-0.99, "as expected"), (1, "ahead"),
                                        (2, "ahead"), (2.01, "well ahead"), (-1, "behind"), (-2, "behind"), (-2.01, "well behind")])
def test_labels(z, expected):
    assert label(z) == expected


def test_more_deaths_are_behind_not_ahead():
    assert label(1.5, lower_is_better=True) == "behind"
    assert label(-2.5, lower_is_better=True) == "well ahead"


def stat(role_result, name):
    return next(s for s in role_result.stats if s.stat == name)


# ---------- test 4: solo and ganked deaths are separate ----------
def test_ganked_deaths_do_not_change_the_solo_death_label():
    game = on_plan()
    quiet = attribute(Flat(), game, 0.5).roles["mid"]
    game.players[BLUE["mid"]]["deaths_ganked_14"] = 4.0
    ganked = attribute(Flat(), game, 0.5).roles["mid"]
    assert stat(ganked, "deaths_ganked").label == "well behind"
    assert stat(ganked, "deaths_solo").label == stat(quiet, "deaths_solo").label == "as expected"


# ---------- chips ----------
def test_each_role_shows_the_stats_that_mean_something_for_it():
    roles = attribute(Flat(), on_plan(), 0.5).roles
    names = {r: [s.stat for s in v.stats] for r, v in roles.items()}
    laner = ["gold", "xp", "cs", "deaths_solo", "deaths_ganked"]
    assert names["top"] == names["mid"] == laner
    assert names["jungle"] == ["jungle_cs", "dragons", "grubs", "kp"]  # no jungle matchup, no herald (decision 18)
    assert names["bot"] == ["gold", "xp"]
    assert names["adc"] == laner
    assert names["support"] == ["gold", "xp", "cs", "vision", "deaths_solo", "deaths_ganked"]
    assert names["objectives"] == ["towers", "plates", "dragons", "grubs"]


def test_the_jungle_label_follows_what_its_share_measures():
    """Its share of Early comes from its gold, so its label does too, or the chip could say "behind" on a positive share."""
    jungle = attribute(Flat(), on_plan(moved={"jungle": 150}), 0.5).roles["jungle"]
    assert jungle.share > 0
    assert jungle.headline.stat == "gold" and jungle.label == "ahead"


def test_a_stat_nobody_recorded_is_left_out_not_zero():
    game = on_plan()
    game.players[BLUE["jungle"]]["kp_15"] = None
    assert "kp" not in [s.stat for s in attribute(Flat(), game, 0.5).roles["jungle"].stats]


def test_a_stat_line_has_actual_expected_sd_and_z():
    top = attribute(Flat(), on_plan(moved={"top": 250}), 0.5).roles["top"]
    gold = stat(top, "gold")
    assert (gold.actual, gold.expected, gold.sd) == (550.0, 300.0, SD)
    assert gold.z == pytest.approx(2.5) and gold.label == "well ahead"


def test_vision_is_per_minute_on_the_support():
    support = attribute(Flat(), on_plan(), 0.5).roles["support"]
    assert stat(support, "vision").actual == pytest.approx(1.0)


def test_the_objectives_label_never_argues_with_its_share():
    """Towers well behind, but the extra dragons are what moved the chance: it says ahead, from the dragons."""
    chip = attribute(Flat(), on_plan(dragons=2.5, red_extra={"towers_15": 3.0}), 0.5).roles["objectives"]
    assert chip.share > 0
    assert (chip.label, chip.headline.stat) == ("ahead", "dragons")
    assert stat(chip, "towers").label == "well behind"  # still there in the table


def test_objectives_that_moved_nothing_are_as_expected_whatever_else_is_far_off():
    chip = attribute(Flat(), on_plan(red_extra={"towers_15": 3.0}), 0.5).roles["objectives"]
    assert chip.share == pytest.approx(0, abs=1e-12)
    assert chip.label == "as expected"


# ---------- contributors ----------
def test_the_part_a_verdict_names_comes_first():
    """Conversion is larger here, but a game called lost in the early game has to say what the early game did."""
    a = attribute(Flat(), on_plan(won=False, moved={"top": -1500}), 0.5)
    assert a.verdict == "Lost in the early game"
    assert abs(a.conversion) > abs(a.early)
    assert "early (Top well behind)" in a.contributors[0] and "conversion" in a.contributors[1]


def test_without_a_named_part_the_largest_comes_first():
    a = attribute(Flat(), on_plan(won=True, moved={"mid": 500}), 0.5)
    assert a.verdict == "Inconclusive"
    assert a.contributors[0].startswith("+45pp conversion")


def test_a_lost_lead_names_the_lane_and_what_happened_after_15():
    game = on_plan(won=False, moved={"mid": 2500}, blue_extra={"deaths_while_ahead": 3.0}, red_extra={"barons": 1.0})
    a = attribute(Flat(), game, 0.5)
    assert a.verdict == "Lost from ahead"
    assert a.line.startswith("Lost from ahead: ")
    assert any("conversion (3 deaths while ahead, Baron lost)" in c for c in a.contributors)
    assert any("early (Mid well ahead)" in c for c in a.contributors)
    assert len(a.contributors) == 2


def test_the_largest_part_comes_first():
    game = on_plan(won=False, moved={"mid": 2500})
    a = attribute(Flat(), game, 0.5)
    assert a.contributors[0].startswith("−") and "conversion" in a.contributors[0]


def test_a_won_game_says_what_went_right_after_15():
    game = on_plan(won=True, moved={"top": -2500}, red_extra={"deaths_while_ahead": 1.0}, blue_extra={"barons": 2.0})
    assert any("(their 1 death while ahead, 2 Barons taken)" in c for c in attribute(Flat(), game, 0.5).contributors)


def test_objectives_can_be_the_early_contributor():
    a = attribute(Flat(), on_plan(won=True, dragons=4.5), 0.5)
    assert any("early (Objectives and team)" in c for c in a.contributors)


def test_wording_stays_neutral():
    a = attribute(Flat(), on_plan(won=False, moved={"mid": -2500}, blue_extra={"deaths_while_ahead": 3.0}), 0.5)
    text = (a.line + " ".join(s.label for s in all_stats(a))).lower()
    assert "fault" not in text and "blame" not in text


def test_conversion_keeps_the_counts_and_gold_at_25():
    game = on_plan(blue_extra={"gold_25": 41_000.0, "barons": 1.0, "dragons_after_15": 2.0, "towers_after_15": 3.0},
                   red_extra={"gold_25": 40_000.0, "dragons_after_15": 1.0})
    detail = attribute(Flat(), game, 0.5).detail
    assert detail["barons"] == [1.0, 0.0] and detail["dragons_after_15"] == [2.0, 1.0] and detail["towers_after_15"] == [3.0, 0.0]
    assert detail["gold_25"]["actual"] == 1000.0
    assert attribute(Flat(), on_plan(), 0.5).detail["gold_25"] is None  # the game ended first


# ---------- P0 ----------
def test_p0_scales_comfort_with_the_draft():
    data, t = synthetic_data()
    engine = DraftEngine(data, t)
    final = {"top": 0, "jungle": 0, "mid": 0, "adc": 0, "support": 0}
    pools = {r: next(iter(p)) for r, p in t.pools.items() if p}
    ours = {**final, **pools}
    used = set(ours.values())
    theirs = {}
    for r in ours:
        theirs[r] = next(c for c in sorted(data.champions()) if c not in used and data.brackets["gold"].role_stat(c, r))
        used.add(theirs[r])
    scorer = DraftScorer(engine, scale=0.2)
    comfort = engine.draft_comfort(ours)
    assert comfort == pytest.approx(sum(engine.cfg.beta * engine._comfort(c, r) for r, c in ours.items()))
    assert scorer.p0(ours, theirs) == pytest.approx(sigmoid(0.2 * (engine.draft_logodds(ours, theirs) + comfort)))


# ---------- the job, on a fitted model ----------
class Scorer:
    brackets = dict.fromkeys(BLUE, "gold")
    team_bracket = "gold"

    def __init__(self, p0=0.5):
        self.value, self.calls = p0, 0

    def p0(self, ours, theirs):
        self.calls += 1
        return self.value

    def roles(self, draft, actions):
        return BLUE, RED  # no timeline to read them from


def add_draft(db, draft_id, side="blue", result="win", game_id=None):
    ours, theirs = (BLUE, RED) if side == "blue" else (RED, BLUE)
    with Session(db) as s:
        s.add(Draft(draft_id=draft_id, game_id=game_id or draft_id, queue_id=440, side=side, result=result,
                    played_at=datetime(2026, 11, 1, tzinfo=UTC), draft_format="tournament", pred_win_prob=0.7,
                    match_verified=True, timeline_version=2))
        seq = 0
        for team_side, comp in ((side, ours), ("red" if side == "blue" else "blue", theirs)):
            for role, champ in comp.items():
                s.add(DraftAction(draft_id=draft_id, seq=seq, side=team_side, type="pick", champ_id=champ,
                                  role=role if team_side == side else None))
                seq += 1
        s.commit()


def add_timeline(db, draft_id, model, moved=None, roles=True):
    """Blue's leads as the fitted model expects, plus `moved`."""
    match_id = f"EUW1_OWN{draft_id}"
    moved = moved or {}
    leads = {r: model.lane("gold", r, BLUE[r], RED[r], "gold").mean + moved.get(r, 0.0) for r in ("top", "jungle", "mid")}
    half = model.bot("gold", BLUE["adc"], BLUE["support"], RED["adc"], RED["support"], "gold").mean / 2
    leads["adc"], leads["support"] = half + moved.get("adc", 0.0), half + moved.get("support", 0.0)
    expected = model.expected_state(BLUE, RED, 0.0, 0.0, "gold")
    with Session(db) as s:
        for role, lead in leads.items():
            for team_id, champ, sign in ((100, BLUE[role], 1), (200, RED[role], -1)):
                s.add(OwnGameParticipant(
                    draft_id=draft_id, champ_id=champ, match_id=match_id, team_id=team_id, role=role if roles else None,
                    lane_gold_15=sign * lead, lane_xp_15=0.0, lane_cs_14=0.0, jungle_cs_15=90.0, kp_15=0.5, deaths_solo_14=0.0,
                    deaths_ganked_14=0.0, plates_14=0.0, wards_placed_15=8.0, wards_killed_15=1.0))
        s.add(MatchTeamState(match_id=match_id, team_id=100, gold_15=25_000 + sum(leads.values()), towers_15=expected["towers"],
                             plates_15=expected["plates"], dragons_15=expected["dragons"], grubs_15=expected["grubs"],
                             herald_15=expected["herald"], barons=1.0))
        s.add(MatchTeamState(match_id=match_id, team_id=200, gold_15=25_000.0, towers_15=0.0, plates_15=0.0, dragons_15=0.0,
                             grubs_15=0.0, herald_15=0.0, barons=0.0))
        s.commit()


@pytest.fixture
def fitted_db(engine):
    _crawl(engine, n=600)
    return engine, fit_execution(engine, draft=lambda match, won: 0.0)


def stored(db, draft_id):
    with Session(db) as s:
        row = s.get(GameExecution, draft_id)
        roles = {r.role: r for r in s.exec(select(GameExecutionRole).where(GameExecutionRole.draft_id == draft_id)).all()}
    return row, roles


def test_the_job_stores_a_game_that_sums_on_a_real_fit(fitted_db):
    db, model = fitted_db
    add_draft(db, 1, "red", "loss")
    add_timeline(db, 1, model, moved={"top": 1500, "mid": -400, "adc": 300})
    assert refresh_executions(db, scorer=Scorer(0.47)) == 1
    row, roles = stored(db, 1)
    assert row.p0 == 0.47 and row.window == model.window
    assert row.draft + row.early + row.conversion == pytest.approx(-0.5, abs=1e-9)
    assert sum(roles[r].share for r in ("top", "jungle", "mid", "bot", "objectives")) == pytest.approx(row.early, abs=1e-9)
    assert set(roles) == {"top", "jungle", "mid", "bot", "adc", "support", "objectives"}
    assert roles["top"].share < 0  # blue's top got ahead, and we were red
    assert roles["top"].label == "well behind"
    stats = json.loads(roles["top"].stats_json)
    assert {"stat", "actual", "expected", "sd", "z", "label"} <= set(stats[0])
    assert row.verdict and json.loads(row.contributors_json)


def test_on_a_real_fit_a_game_on_plan_has_no_early_part(fitted_db):
    db, model = fitted_db
    add_draft(db, 1, "blue", "win")
    add_timeline(db, 1, model)
    refresh_executions(db, scorer=Scorer(0.5))
    row, roles = stored(db, 1)
    assert row.early == pytest.approx(0, abs=1e-6)
    assert all(abs(r.share) < 1e-6 for r in roles.values())


def test_stored_numbers_hold_no_names_or_ids(fitted_db):
    db, model = fitted_db
    add_draft(db, 1)
    add_timeline(db, 1, model)
    refresh_executions(db, scorer=Scorer())
    row, roles = stored(db, 1)
    blob = row.model_dump_json() + "".join(r.model_dump_json() for r in roles.values())
    assert "puuid" not in blob.lower() and "riot_id" not in blob.lower()


def test_a_game_without_a_timeline_stores_the_draft_only(fitted_db):
    db, _ = fitted_db
    add_draft(db, 1, "blue", "loss")
    refresh_executions(db, scorer=Scorer(0.44))
    row, roles = stored(db, 1)
    assert row.draft == pytest.approx(-0.06)
    assert (row.p15, row.early, row.conversion, row.verdict) == (None, None, None, None)
    assert roles == {}


def test_a_game_under_15_minutes_stores_the_draft_only(fitted_db):
    db, _ = fitted_db
    add_draft(db, 1)
    with Session(db) as s:  # as store_own_timeline leaves it: ten rows of NULLs, no team state
        for comp, team_id in ((BLUE, 100), (RED, 200)):
            for role, champ in comp.items():
                s.add(OwnGameParticipant(draft_id=1, champ_id=champ, match_id="EUW1_OWN1", team_id=team_id, role=role))
        s.commit()
    refresh_executions(db, scorer=Scorer())
    assert stored(db, 1)[0].p15 is None


def test_a_game_with_no_known_positions_stores_the_draft_only(fitted_db):
    db, model = fitted_db
    add_draft(db, 1)
    add_timeline(db, 1, model, roles=False)
    refresh_executions(db, scorer=Scorer())
    assert stored(db, 1)[0].p15 is None


def test_a_game_with_no_result_or_too_few_picks_is_skipped(fitted_db):
    db, _ = fitted_db
    add_draft(db, 1, result=None)
    with Session(db) as s:
        s.add(Draft(draft_id=2, side="blue", result="win", played_at=datetime(2026, 11, 1, tzinfo=UTC)))
        s.commit()
    assert refresh_executions(db, scorer=Scorer()) == 0


def test_missing_only_fills_what_is_new_and_a_timeline_that_arrived(fitted_db):
    db, model = fitted_db
    add_draft(db, 1)
    add_timeline(db, 1, model)
    add_draft(db, 2)
    scorer = Scorer()
    assert refresh_executions(db, scorer=scorer) == 2
    assert refresh_executions(db, only_missing=True, scorer=scorer) == 0
    assert scorer.calls == 2
    add_timeline(db, 2, model)  # verification caught up
    assert refresh_executions(db, only_missing=True, scorer=scorer) == 1
    assert stored(db, 2)[0].p15 is not None


def test_a_refit_recomputes_every_game(fitted_db):
    db, model = fitted_db
    add_draft(db, 1)
    add_timeline(db, 1, model, moved={"top": 1000})
    refresh_executions(db, scorer=Scorer(0.5))
    first = stored(db, 1)[0].p0
    assert refresh_executions(db, scorer=Scorer(0.6)) == 1
    assert stored(db, 1)[0].p0 == 0.6 != first


def test_no_fit_means_nothing_to_do(engine):
    add_draft(engine, 1)
    assert refresh_executions(engine, scorer=Scorer()) == 0
    with Session(engine) as s:
        assert s.get(GameExecution, 1) is None


def test_a_remake_is_not_attributed(fitted_db):
    db, _ = fitted_db
    add_draft(db, 1, result="remake")
    assert refresh_executions(db, scorer=Scorer()) == 0


def test_verifying_a_game_fills_in_its_split_straight_away(engine, monkeypatch):
    """Its timeline only arrives here, after the learning job has run for that game."""
    import threading

    from draftassist.data import postgame
    from draftassist.learning import execution

    done = threading.Event()
    monkeypatch.setattr(postgame, "verify_draft", lambda db, riot, draft_id: "verified")
    monkeypatch.setattr(execution, "refresh_executions", lambda db, only_missing=False: done.set() if only_missing else None)
    postgame.schedule_verification(engine, lambda: None, 1, delays=(0.0,))
    assert done.wait(5)
