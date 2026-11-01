"""Legal, non-wasted ban suggestions (docs/prompts: ban guards). Strict mode is on for all tests."""
import logging

import pytest

from draftassist.engine.engine import DraftEngine, Suggestion
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.guards import GuardViolation, validate_suggestions
from draftassist.engine.model import BracketStats, EngineData, OurTeam, RoleStat
from draftassist.engine.state import SKIPPED, DraftState

(TOP1, TOP2, JG1, JG2, MID1, MID2, MID3, FLEX, ADC1, ADC2, SUP1, SUP2) = range(1, 13)
NAMES = {TOP1: "Top1", TOP2: "Top2", JG1: "Jg1", JG2: "Jg2", MID1: "Mid1", MID2: "Mid2", MID3: "Mid3",
         FLEX: "Taliyah", ADC1: "Adc1", ADC2: "Adc2", SUP1: "Sup1", SUP2: "Sup2"}
SINGLE_ROLE = {TOP1: "top", TOP2: "top", JG1: "jungle", JG2: "jungle", MID1: "mid", MID2: "mid", MID3: "mid",
               ADC1: "adc", ADC2: "adc", SUP1: "support", SUP2: "support"}


def _stats():
    meta = {c: {r: RoleStat(0.52, 0.08)} for c, r in SINGLE_ROLE.items()}
    meta[FLEX] = {"mid": RoleStat(0.52, 0.06), "jungle": RoleStat(0.52, 0.04)}  # 60/40 flex
    matchups = {
        ("jungle", FLEX, JG1): 0.56,  # Taliyah beats our jungler
        ("jungle", JG2, JG1): 0.48,
        ("mid", MID3, MID1): 0.57,  # Mid3 counters our Mid1
        ("mid", MID2, MID1): 0.50,
        ("support", SUP2, SUP1): 0.55,
        ("top", TOP2, TOP1): 0.54,
        ("adc", ADC2, ADC1): 0.53,
    }
    return BracketStats(meta, matchups)


DATA = EngineData({"gold": _stats()}, NAMES)
TEAM = OurTeam.from_tiers(
    {"top": {TOP1: "main"}, "jungle": {JG1: "main"}, "mid": {MID1: "main", FLEX: "comfortable"},
     "adc": {ADC1: "main"}, "support": {SUP1: "main"}},
    {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")},
)
SIX_SKIPS = (SKIPPED,) * 6
ALL = 100  # k large enough to see every candidate


@pytest.fixture
def engine():
    return DraftEngine(DATA, TEAM)


def tstate(*actions, hovers=()):
    return DraftState(TOURNAMENT, "blue", tuple(actions), hovers=tuple(hovers))


def ids(suggestions):
    return [s.champ_id for s in suggestions]


def all_lists(engine, state):
    lists = {"ban": engine.suggest_bans(state, k=ALL), "enemy": engine.likely_enemy_picks(state, k=ALL)}
    if len(state.our_picks) < 5:
        lists["pick"] = engine.suggest_picks(state, k=ALL)
    return lists


# 1. Banned / picked never suggested --------------------------------------------
@pytest.mark.parametrize("actions", [
    (MID3,),                              # banned by us (blue ban 1)
    (SKIPPED, MID3),                      # banned by them
    (*SIX_SKIPS, MID3),                   # picked by us
    (*SIX_SKIPS, TOP1, MID3),             # picked by them
], ids=["banned-by-us", "banned-by-them", "picked-by-us", "picked-by-them"])
def test_unavailable_never_suggested_in_any_list(engine, actions):
    state = tstate(*actions)
    assert MID3 in state.unavailable()
    for name, sugs in all_lists(engine, state).items():
        assert MID3 not in ids(sugs), f"{name} list suggested an unavailable champion"


# 2. Skipped bans -----------------------------------------------------------------
def test_skipped_bans_do_not_break_availability(engine):
    state = tstate(SKIPPED, SKIPPED, MID3)
    assert state.unavailable() == frozenset({MID3})
    assert state.step.index == 3 and state.is_our_turn is False
    for sugs in all_lists(engine, state).values():
        assert SKIPPED not in ids(sugs) and MID3 not in ids(sugs)


# 3. Own-pool ban allowed ---------------------------------------------------------
def test_own_pool_champion_can_be_banned(engine):
    bans = engine.suggest_bans(tstate(), k=ALL)
    assert FLEX in ids(bans)  # in our mid pool, and a threat to our jungler


# 4. Hover ----------------------------------------------------------------------
def test_hovered_champion_can_be_banned_with_warning(engine):
    bans = {s.champ_id: s for s in engine.suggest_bans(tstate(hovers=[(FLEX, "mid")]), k=ALL)}
    assert FLEX in bans
    assert bans[FLEX].warnings == ("⚠ hovered by Mid",)
    assert all(not s.warnings for c, s in bans.items() if c != FLEX)


def test_hover_is_not_a_pick():
    state = tstate(hovers=[(FLEX, "mid")])
    assert FLEX not in state.unavailable()
    assert state.apply(SKIPPED).hovers == ()  # hovers are per-event; the next update re-sends them


# 5. Role waste -----------------------------------------------------------------
def phase2(blue_picks, red_picks, hovers=()):
    b1, b2, b3 = blue_picks
    r1, r2, r3 = red_picks
    return tstate(*SIX_SKIPS, b1, r1, r2, b2, b3, r3, SKIPPED, hovers=hovers)  # step 14: blue's ban


def test_no_bans_on_roles_the_enemy_filled(engine):
    state = phase2((TOP1, JG1, ADC1), (MID2, JG2, ADC2))
    assert state.step.label == "Ban 2" and state.is_our_turn
    bans = engine.suggest_bans(state, k=ALL)
    assert bans
    for s in bans:
        assert SINGLE_ROLE.get(s.champ_id) not in ("mid", "jungle", "adc"), s
    assert FLEX not in ids(bans)  # both of its roles are filled


# 6. Flex survives while one of its roles is open ---------------------------------
def test_flex_keeps_open_role_share(engine):
    state = phase2((TOP1, JG1, ADC1), (MID2, TOP2, ADC2))  # enemy mid filled, jungle open
    bans = {s.champ_id: s for s in engine.suggest_bans(state, k=ALL)}
    assert FLEX in bans
    assert bans[FLEX].relevance == pytest.approx(0.4, abs=0.01)
    assert MID3 not in bans

    both_filled = phase2((TOP1, JG1, ADC1), (MID2, JG2, ADC2))
    assert FLEX not in ids(engine.suggest_bans(both_filled, k=ALL))


# 7. Ambiguous enemy flex pick ------------------------------------------------------
def test_enemy_flex_pick_splits_filled_probability(engine):
    state = tstate(*SIX_SKIPS, TOP1, FLEX)  # enemy's only pick is the 60/40 mid/jungle flex
    filled = engine.role_filled_probs(state)
    assert filled["mid"] == pytest.approx(0.6, abs=0.01)
    assert filled["jungle"] == pytest.approx(0.4, abs=0.01)
    assert filled["top"] < 0.01
    rel = engine.ban_relevance(state)
    assert rel[MID3] == pytest.approx(0.4, abs=0.01)
    assert rel[JG2] == pytest.approx(0.6, abs=0.01)
    assert rel[SUP2] == pytest.approx(1.0, abs=0.01)


# 8. Our filled roles don't block bans ----------------------------------------------
def test_ban_can_protect_our_locked_pick(engine):
    state = phase2((MID1, JG1, SUP1), (TOP2, ADC2, SUP2))  # our mid locked, enemy mid open
    bans = {s.champ_id: s for s in engine.suggest_bans(state, k=ALL)}
    assert MID3 in bans  # counters our Mid1
    assert bans[MID3].relevance == pytest.approx(1.0, abs=0.01)


# 9. Fewer than three ----------------------------------------------------------------
def test_returns_fewer_than_three_rather_than_padding(engine):
    # Enemy filled top/mid/adc; of the jungle and support champions only Sup2 is left.
    state = tstate(JG2, SKIPPED, FLEX, SKIPPED, SKIPPED, SKIPPED,
                   JG1, TOP2, MID2, SUP1, TOP1, ADC2, SKIPPED)
    assert state.step.label == "Ban 2" and state.is_our_turn
    bans = engine.suggest_bans(state, k=3)
    assert ids(bans) == [SUP2]


def test_never_suggests_a_ban_with_no_value(engine):
    for state in (tstate(), phase2((TOP1, JG1, ADC1), (MID2, TOP2, ADC2))):
        bans = engine.suggest_bans(state, k=ALL)
        assert all(s.value > 0 for s in bans)
        assert len(set(ids(bans))) == len(bans)


# 10. Guard ----------------------------------------------------------------------------
def _sug(champ, kind="ban", value=0.1, relevance=1.0):
    return Suggestion(champ, NAMES.get(champ, "?"), kind, value, "test", relevance=relevance)


def test_guard_raises_in_strict_mode():
    state = tstate(MID3)
    with pytest.raises(GuardViolation, match="unavailable"):
        validate_suggestions(state, "ban", [_sug(TOP2), _sug(MID3)])
    with pytest.raises(GuardViolation, match="duplicate"):
        validate_suggestions(state, "pick", [_sug(TOP2, "pick"), _sug(TOP2, "pick")])
    with pytest.raises(GuardViolation, match="relevance"):
        validate_suggestions(state, "ban", [_sug(TOP2, relevance=0.05)])
    with pytest.raises(GuardViolation, match="value"):
        validate_suggestions(state, "ban", [_sug(TOP2, value=0.0)])


def test_guard_drops_and_logs_in_normal_mode(monkeypatch, caplog):
    monkeypatch.delenv("DRAFTASSIST_STRICT", raising=False)
    state = tstate(MID3)
    with caplog.at_level(logging.WARNING, logger="draftassist.engine.guards"):
        kept = validate_suggestions(state, "ban", [_sug(TOP2), _sug(MID3), _sug(TOP2)])
    assert ids(kept) == [TOP2]
    assert "unavailable" in caplog.text and "duplicate" in caplog.text
    assert "actions=" in caplog.text  # the draft state is in the log line
