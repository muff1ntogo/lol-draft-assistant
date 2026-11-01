import pytest

from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT, get_format
from draftassist.engine.state import SKIPPED, DraftState


def order(fmt, kind=None):
    return " ".join(s.side[0].upper() for s in fmt.steps if kind in (None, s.kind))


def test_tournament_order_matches_spec_appendix():
    assert len(TOURNAMENT) == 20
    by_label = {}
    for s in TOURNAMENT.steps:
        by_label.setdefault(s.label, []).append(s.side[0].upper())
    assert by_label == {
        "Ban 1": list("BRBRBR"),
        "Pick 1": list("BRRBBR"),
        "Ban 2": list("RBRB"),
        "Pick 2": list("RBBR"),
    }
    assert [s.phase for s in TOURNAMENT.steps] == [1] * 12 + [2] * 8


def test_standard_ranked_order():
    assert len(STANDARD_RANKED) == 20
    assert order(STANDARD_RANKED, "ban") == "B R B R B R B R B R"
    assert order(STANDARD_RANKED, "pick") == "B R R B B R R B B R"
    assert {s.phase for s in STANDARD_RANKED.steps} == {1}


@pytest.mark.parametrize("fmt", [TOURNAMENT, STANDARD_RANKED])
def test_each_side_bans_five_and_picks_five(fmt):
    for side in ("blue", "red"):
        assert sum(1 for s in fmt.steps if s.side == side and s.kind == "ban") == 5
        assert sum(1 for s in fmt.steps if s.side == side and s.kind == "pick") == 5


def test_get_format():
    assert get_format("tournament") is TOURNAMENT
    assert get_format("ranked") is STANDARD_RANKED
    with pytest.raises(ValueError, match="unknown draft format"):
        get_format("fearless")


def test_state_progression_and_skipped_ban():
    s = DraftState(TOURNAMENT, "red")
    assert s.step.index == 0 and not s.is_our_turn
    s = s.apply(SKIPPED)  # blue skips a ban: the turn still advances
    assert s.step.index == 1 and s.is_our_turn
    s = s.apply(10)
    assert s.bans() == [10] and s.bans("red") == [10] and s.bans("blue") == []
    assert s.unavailable() == frozenset({10})


def test_picks_are_attributed_by_side():
    s = DraftState(TOURNAMENT, "red", tuple([SKIPPED] * 6))
    for champ in (1, 2, 3, 4, 5, 6):  # B R R B B R
        s = s.apply(champ)
    assert s.picks("blue") == [1, 4, 5]
    assert s.our_picks == [2, 3, 6] and s.their_picks == [1, 4, 5]


def test_picks_left():
    s = DraftState(TOURNAMENT, "blue", tuple([SKIPPED] * 6))  # at step 7, blue's first pick
    assert s.picks_left("red") == 5
    assert s.picks_left("blue") == 4  # excludes the current step
    assert s.picks_left("blue", after=5) == 5


def test_apply_rejects_illegal_actions():
    s = DraftState(TOURNAMENT, "blue").apply(10)
    with pytest.raises(ValueError, match="already banned or picked"):
        s.apply(10)
    picking = DraftState(TOURNAMENT, "blue", tuple([SKIPPED] * 6))
    with pytest.raises(ValueError, match="only a ban"):
        picking.apply(SKIPPED)
    done = DraftState(TOURNAMENT, "blue", tuple([SKIPPED] * 10 + list(range(1, 11))))
    assert done.is_complete and done.step is None
    with pytest.raises(ValueError, match="complete"):
        done.apply(99)
