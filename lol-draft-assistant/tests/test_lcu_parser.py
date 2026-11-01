import copy
import json
from pathlib import Path

import pytest

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.simulate import synthetic_data
from draftassist.lcu.demo import demo_sessions, expected_states
from draftassist.lcu.parser import SessionParseError, detect_format, parse_session

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "lcu" / "sample-tournament"


def load_sample():
    return json.loads((SAMPLE / "0001-session.json").read_text(encoding="utf-8"))


def test_hand_built_sample_session():
    expected = json.loads((SAMPLE / "expected.json").read_text(encoding="utf-8"))["states"][0]
    live = parse_session(load_sample())
    st = live.state
    assert st.format is TOURNAMENT and st.our_side == "red"
    assert list(st.actions) == expected["actions"]
    assert [list(h) for h in st.hovers] == expected["hovers"]
    assert live.actor_role == expected["actor_role"] and live.seconds_left == expected["seconds_left"]
    assert st.bans("blue") == [268, 222] and st.bans("red") == [150, 254, 497]  # skipped ban dropped
    assert st.their_picks == [103] and st.our_picks == []
    assert st.step.label == "Pick 1" and st.is_our_turn
    assert [s.role for s in live.our_slots] == ["top", "jungle", "mid", "adc", "support"]
    assert live.our_slots[0].is_local and live.our_slots[0].champ_id == 0  # K'Sante is a hover, not a pick
    assert live.their_slots[0].champ_id == 103


def test_selected_champion_counts_as_locked_only_after_completed_pick():
    session = load_sample()
    for turn in session["actions"]:
        for a in turn:
            if a["id"] == 8:
                a["completed"], a["isInProgress"] = True, False
            if a["id"] == 9:
                a["isInProgress"] = True
    live = parse_session(session)
    assert live.state.our_picks == [897]
    assert live.our_slots[0].champ_id == 897 and live.our_slots[0].hover_id == 0
    assert (897, "top") not in live.state.hovers
    assert live.actor_role == "jungle"


@pytest.mark.parametrize("field", ["actions", "myTeam", "theirTeam", "timer", "localPlayerCellId"])
def test_missing_top_level_field_fails_loudly(field):
    session = load_sample()
    del session[field]
    with pytest.raises(SessionParseError, match=field):
        parse_session(session)


def test_renamed_action_field_fails_loudly():
    session = load_sample()
    session["actions"][0][0]["actorCellID"] = session["actions"][0][0].pop("actorCellId")
    session["actions"][0][0]["completed"] = False
    session["actions"][0][0]["isInProgress"] = True
    with pytest.raises(SessionParseError, match="actorCellId"):
        parse_session(session)


def test_format_detection():
    assert detect_format(load_sample()) is TOURNAMENT
    ranked = copy.deepcopy(load_sample())
    ranked["actions"] = [[{**a, "type": "ban"} for a in ranked["actions"][0] * 2]]
    assert detect_format(ranked) is STANDARD_RANKED


@pytest.mark.parametrize("fmt", [TOURNAMENT, STANDARD_RANKED], ids=lambda f: f.name)
@pytest.mark.parametrize("side", ["blue", "red"])
def test_demo_draft_round_trips(fmt, side):
    data, team = synthetic_data()
    events = demo_sessions(DraftEngine(data, team), fmt, side)
    expected = expected_states(events, fmt, side)
    prev = ()
    hovers_seen = 0
    for event, exp in zip(events, expected):
        live = parse_session(event)
        assert live.state.format is fmt and live.state.our_side == side
        assert live.state.actions == exp.actions
        assert live.state.actions[: len(prev)] == prev  # the draft only ever grows
        prev = live.state.actions
        hovers_seen += bool(live.state.hovers)
        for champ, role in live.state.hovers:
            assert champ not in live.state.unavailable() and role in ("top", "jungle", "mid", "adc", "support")
    assert len(prev) == 20 and hovers_seen == 5
    assert parse_session(events[-1]).timer_phase == "FINALIZATION"
