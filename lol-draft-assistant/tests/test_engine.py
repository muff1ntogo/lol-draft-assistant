import math

import pytest

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import (
    BracketStats,
    EngineConfig,
    EngineData,
    LaneBanConfig,
    OurTeam,
    RoleStat,
    logit,
    sigmoid,
    tier_comfort,
)
from draftassist.engine.state import SKIPPED, DraftState

KSANTE, GNAR, RUMBLE, SEJUANI, VI, AZIR, ORIANNA, KAISA, VARUS, NAUTILUS, RAKAN, RELL = range(1, 13)
NAMES = {KSANTE: "K'Sante", GNAR: "Gnar", RUMBLE: "Rumble", SEJUANI: "Sejuani", VI: "Vi", AZIR: "Azir",
         ORIANNA: "Orianna", KAISA: "Kai'Sa", VARUS: "Varus", NAUTILUS: "Nautilus", RAKAN: "Rakan", RELL: "Rell"}


def rs(wr, pr=0.05):
    return RoleStat(wr, pr)


def bracket(support_wr=0.51):
    meta = {
        KSANTE: {"top": rs(0.50, 0.10)},
        GNAR: {"top": rs(0.51, 0.08)},
        RUMBLE: {"top": rs(0.50, 0.04), "mid": rs(0.49, 0.02)},
        SEJUANI: {"jungle": rs(0.52, 0.09)},
        VI: {"jungle": rs(0.51, 0.06)},
        AZIR: {"mid": rs(0.48, 0.05)},
        ORIANNA: {"mid": rs(0.51, 0.08)},
        KAISA: {"adc": rs(0.50, 0.12)},
        VARUS: {"adc": rs(0.51, 0.07)},
        NAUTILUS: {"support": rs(support_wr, 0.10)},
        RAKAN: {"support": rs(0.51, 0.05)},
        RELL: {"support": rs(0.51, 0.04)},
    }
    matchups = {("top", GNAR, KSANTE): 0.56, ("mid", AZIR, ORIANNA): 0.55}
    synergy = {frozenset((KAISA, NAUTILUS)): 0.55}
    return BracketStats(meta, matchups, synergy)


DATA = EngineData({"gold": bracket(), "iron": bracket(support_wr=0.54)}, NAMES)
BRACKETS = {"top": "gold", "jungle": "gold", "mid": "gold", "adc": "gold", "support": "iron"}
TEAM = OurTeam.from_tiers(
    {
        "top": {KSANTE: "main", RUMBLE: "comfortable"},
        "jungle": {SEJUANI: "main"},
        "mid": {AZIR: "main", ORIANNA: "comfortable", RUMBLE: "learning"},
        "adc": {KAISA: "comfortable"},
        "support": {NAUTILUS: "main", RAKAN: "comfortable"},
    },
    BRACKETS,
)


@pytest.fixture
def engine():
    return DraftEngine(DATA, TEAM)


def state(*actions, side="blue"):
    return DraftState(TOURNAMENT, side, tuple(actions))


SIX_SKIPPED_BANS = (SKIPPED,) * 6


def test_tier_comfort_from_manual_tier():
    assert tier_comfort("main") == pytest.approx(0.18)
    assert tier_comfort("comfortable") == pytest.approx(0.0)
    assert tier_comfort("learning") == pytest.approx(-0.24)


def test_empty_draft_is_a_coin_flip(engine):
    assert engine.win_prob(state()) == pytest.approx(0.5)


def test_score_terms_for_one_pick_each(engine):
    s = state(*SIX_SKIPPED_BANS, KSANTE, GNAR)
    ev = engine.evaluate(s)
    assert ev.our_roles == {KSANTE: "top"} and ev.their_roles == {GNAR: "top"}
    t = ev.terms
    assert t.meta_us == pytest.approx(logit(0.50))
    assert t.comfort == pytest.approx(tier_comfort("main"))
    assert t.meta_them == pytest.approx(logit(0.51))
    assert t.lane == pytest.approx(logit(0.44))  # Gnar's 56% read from K'Sante's side
    assert ev.win_prob == pytest.approx(sigmoid(t.total))


def test_our_meta_uses_each_players_bracket(engine):
    t = engine.evaluate(state(*SIX_SKIPPED_BANS, NAUTILUS)).terms
    assert t.meta_us == pytest.approx(logit(0.54))  # support plays at Iron


def test_off_pool_penalty_and_weights():
    s = state(*SIX_SKIPPED_BANS, GNAR)  # Gnar is not in our pool
    assert DraftEngine(DATA, TEAM).evaluate(s).terms.comfort == pytest.approx(-0.4)
    no_comfort = DraftEngine(DATA, TEAM, EngineConfig(beta=0.0)).evaluate(s).terms
    assert no_comfort.comfort == 0.0
    double_meta = DraftEngine(DATA, TEAM, EngineConfig(alpha=2.0)).evaluate(s).terms
    assert double_meta.meta_us == pytest.approx(2 * logit(0.51))


def test_role_assignment_prefers_comfort(engine):
    # Rumble plays top (comfortable) or mid (learning); with K'Sante taking top it goes mid.
    assert engine.evaluate(state(*SIX_SKIPPED_BANS, RUMBLE)).our_roles == {RUMBLE: "top"}
    s = state(*SIX_SKIPPED_BANS, KSANTE, GNAR, VI, RUMBLE)
    assert engine.evaluate(s).our_roles == {KSANTE: "top", RUMBLE: "mid"}


def test_synergy_counts_for_our_pairs(engine):
    s = state(*SIX_SKIPPED_BANS, KAISA, GNAR, VI, NAUTILUS)
    assert engine.evaluate(s).terms.syn_us == pytest.approx(logit(0.55))


def test_enemy_probs_are_a_distribution_over_open_roles(engine):
    s = state(*SIX_SKIPPED_BANS, KSANTE, GNAR)  # enemy has filled top
    probs = engine.enemy_pick_probs(s)
    assert sum(probs.values()) == pytest.approx(1.0)
    assert KSANTE not in probs and GNAR not in probs
    # Rumble can only go mid now; pure top champions get nothing.
    assert RUMBLE in probs
    # Same win rate, higher pick rate -> more likely.
    assert probs[RAKAN] > probs[RELL]


def test_enemy_probs_empty_when_they_have_no_picks_left(engine):
    full_red = state(*SIX_SKIPPED_BANS, 1, 2, 3, 4, 5, 6, SKIPPED, SKIPPED, SKIPPED, SKIPPED, 7, 8, 9, 10)
    assert full_red.is_complete
    assert engine.enemy_pick_probs(full_red) == {}


def test_suggest_rejects_enemy_turn_and_complete_draft(engine):
    with pytest.raises(ValueError, match="not our turn"):
        engine.suggest(state(SKIPPED))
    with pytest.raises(ValueError, match="complete"):
        engine.suggest(state(*SIX_SKIPPED_BANS, *range(1, 7), *[SKIPPED] * 4, 7, 8, 9, 10))


def test_pick_reason_names_role_tier_and_counter(engine):
    # Red: our Azir (mid main) would counter their Orianna.
    s = state(*SIX_SKIPPED_BANS, ORIANNA, side="red")
    sugs = engine.suggest_picks(s, k=20, role="mid")
    azir = next(x for x in sugs if x.champ_id == AZIR)
    assert azir.reason == "Mid main, counters their Orianna"
    assert azir.role == "mid"


def test_pick_role_restriction_and_filled_roles(engine):
    s = state(*SIX_SKIPPED_BANS, KSANTE, GNAR, VI, SEJUANI)  # blue has top and jungle
    sugs = engine.suggest_picks(s, k=50)
    assert sugs and all(x.role not in ("top", "jungle") for x in sugs)
    support_only = engine.suggest_picks(s, k=50, role="support")
    assert support_only and {x.role for x in support_only} == {"support"}
    with pytest.raises(ValueError, match="already filled"):
        engine.suggest_picks(s, role="top")


def test_counter_risk_warning_and_flex(engine):
    sugs = {x.champ_id: x for x in engine.suggest_picks(state(*SIX_SKIPPED_BANS), k=50)}
    # Rumble is in our top and mid pools: flex bonus over its plain win prob.
    assert sugs[RUMBLE].value > sugs[RUMBLE].win_prob
    # Gnar is still open to the enemy and counters K'Sante.
    assert sugs[KSANTE].value < sugs[KSANTE].win_prob
    assert sugs[KSANTE].reason.endswith("watch for Gnar")


def test_ban_reason_uses_lane_ban_score(engine):
    # Gnar is common in top and our best answer (K'Sante) loses the lane 44-56.
    sugs = engine.suggest_bans(state(), k=50)
    gnar = next(x for x in sugs if x.champ_id == GNAR)
    assert gnar.reason == "Common vs our top (PR 8%); our best answer K'Sante only −6.0pp"
    assert gnar.enemy_prob > 0


def test_lane_term_weight_gamma():
    s = state()
    on = {x.champ_id: x for x in DraftEngine(DATA, TEAM).suggest_bans(s, k=50)}
    off = {x.champ_id: x for x in DraftEngine(DATA, TEAM, EngineConfig(lane_ban=LaneBanConfig(gamma=0.0))).suggest_bans(s, k=50)}
    assert on[GNAR].value > off[GNAR].value
    assert off[GNAR].reason.startswith("Their Gnar: ")


def test_lane_ban_tables_cover_all_roles(engine):
    tables = engine.lane_ban_tables(state())
    assert set(tables) == {"top", "jungle", "mid", "adc", "support"}
    top = tables["top"]
    assert top.row(GNAR).via == KSANTE and top.row(GNAR).resp == pytest.approx(-6.0)


def test_phase2_bans_only_target_enemy_open_roles(engine):
    # Red has picked top, jungle, mid; blue bans in phase 2 (step 14).
    s = state(*SIX_SKIPPED_BANS, KSANTE, GNAR, SEJUANI, VI, KAISA, AZIR, SKIPPED)
    assert s.step.label == "Ban 2" and s.is_our_turn
    sugs = engine.suggest_bans(s, k=50)
    assert sugs
    assert all(x.role in ("adc", "support") and x.enemy_prob > 0 for x in sugs)


def test_suggestions_are_deterministic():
    s = state(*SIX_SKIPPED_BANS, KSANTE, GNAR)
    a = DraftEngine(DATA, TEAM).suggest(s.apply(VI), k=5)
    b = DraftEngine(DATA, TEAM).suggest(s.apply(VI), k=5)
    assert a == b
    assert [x.value for x in a] == sorted((x.value for x in a), reverse=True)
    assert all(math.isfinite(x.value) for x in a)


def test_bracket_without_stats_falls_back_instead_of_crashing(caplog):
    team = OurTeam.from_tiers({"adc": {KAISA: "main"}}, {**BRACKETS, "adc": "platinum"})
    eng = DraftEngine(DATA, team)  # DATA has gold and iron only
    assert eng.team.brackets["adc"] == "gold"
    assert "platinum" in caplog.text
    assert eng.suggest(state(*SIX_SKIPPED_BANS), k=3)
