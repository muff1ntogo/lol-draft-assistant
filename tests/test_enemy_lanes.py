"""ui-engine-review.md, Stage 2: role chances for their locked picks and likely picks per open role."""
import pytest

from draftassist.data.scout import ONE_TRICK_GAMES, PlayerPool, scout_summary
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import DraftState


@pytest.fixture(scope="module")
def eng():
    data, team = synthetic_data()
    return DraftEngine(data, team)


def _flex_and_pure(eng):
    shares = eng.role_share_map
    flex = next(c for c, s in shares.items() if len(s) == 2 and max(s.values()) < 0.7)
    pure = next(c for c, s in shares.items() if len(s) == 1 and set(s) != set(shares[flex]))
    return flex, pure


def _with_their_picks(eng, champs):
    """A tournament draft (we're blue) up to their len(champs)-th pick: bans skipped, our picks any free champion."""
    st = DraftState(TOURNAMENT, "blue")
    spare = (c for c in eng.champion_ids if c not in champs)
    while len(st.their_picks) < len(champs):
        step = st.step
        if step.kind == "ban":
            st = st.apply(0)
        elif step.side == "red":
            st = st.apply(champs[len(st.their_picks)])
        else:
            st = st.apply(next(spare))
    return st


def test_role_probs_sum_to_one_and_flex_picks_split(eng):
    flex, pure = _flex_and_pure(eng)
    st = _with_their_picks(eng, [flex])
    probs = eng.their_role_probs(st)[flex]
    assert sum(probs.values()) == pytest.approx(1.0)
    assert max(probs.values()) < 0.85 and len([p for p in probs.values() if p > 0.05]) == 2


def test_a_known_role_takes_the_flex_picks_other_role(eng):
    flex, _ = _flex_and_pure(eng)
    flex_roles = sorted(eng.role_shares(flex), key=lambda r: -eng.role_shares(flex)[r])
    other = next(c for c, s in eng.role_share_map.items() if set(s) == {flex_roles[0]})
    st = _with_their_picks(eng, [other, flex])
    assert eng.their_role_probs(st)[flex][flex_roles[1]] > 0.95


def test_client_positions_are_certain(eng):
    flex, _ = _flex_and_pure(eng)
    st = _with_their_picks(eng, [flex])
    role = min(eng.role_shares(flex), key=lambda r: eng.role_shares(flex)[r])
    st = DraftState(st.format, st.our_side, st.actions, their_positions=((flex, role),))
    assert eng.their_role_probs(st)[flex] == {role: 1.0}


def test_likely_by_role_covers_open_roles_only(eng):
    flex, pure = _flex_and_pure(eng)
    st = _with_their_picks(eng, [pure])
    likely = eng.likely_by_role(st, 3)
    taken = eng.evaluate(st).their_roles[pure]
    assert taken not in likely and len(likely) == 4
    for picks in likely.values():
        assert len(picks) == 3 and all(c not in st.unavailable() for c, _ in picks)
        assert [p for _, p in picks] == sorted((p for _, p in picks), reverse=True)


def test_one_trick_and_mastery_only_labels():
    card = lambda c: {"id": c, "name": str(c)}
    otp = PlayerPool("a#1", "done", games={"mid": {7: (ONE_TRICK_GAMES, 5), 8: (2, 1)}})
    spread = PlayerPool("b#1", "done", games={"mid": {7: (4, 2), 8: (4, 2), 9: (4, 2)}})
    mastery = PlayerPool("c#1", "done", mastery=((7, 100000),))
    out = scout_summary(["a#1", "b#1", "c#1"], {"a#1": otp, "b#1": spread, "c#1": mastery},
                        {"a#1": "mid", "b#1": "mid", "c#1": "top"}, card, sure={"a#1": 1.0, "b#1": 0.6})
    assert out[0]["one_trick"]["id"] == 7 and out[1]["one_trick"] is None
    assert out[2]["mastery_only"] is True and out[0]["mastery_only"] is False
    assert out[1]["role_sure"] == 0.6 and out[2]["role_sure"] == 1.0
