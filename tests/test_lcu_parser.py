import copy
import json
from pathlib import Path

import pytest

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT, DraftFormat, Side
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import DraftState
from draftassist.lcu.demo import demo_sessions
from draftassist.lcu.parser import SessionParseError, detect_format, parse_session

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "lcu" / "sample-tournament"


def expected_states(events: list[dict], fmt: DraftFormat, our_side: Side) -> list[DraftState]:
    """Actions encoded in each event (for round-trip tests)."""
    out = []
    for e in events:
        done = sorted((a for turn in e["actions"] for a in turn if a["completed"]), key=lambda a: a["id"])
        out.append(DraftState(fmt, our_side, tuple(a["championId"] for a in done)))
    return out


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
    ranked["actions"] = [[{**a, "type": "ban"} for a in ranked["actions"][0] * 2]]  # no picks listed
    assert detect_format(ranked) is STANDARD_RANKED


def split_turns(event):
    """The same session with every action in its own turn (client grouping is undocumented)."""
    return {**event, "actions": [[a] for turn in event["actions"] for a in turn]}


@pytest.mark.parametrize("fmt", [TOURNAMENT, STANDARD_RANKED], ids=lambda f: f.name)
@pytest.mark.parametrize("grouping", ["grouped", "one-per-turn"])
def test_format_detected_however_turns_are_grouped(fmt, grouping):
    """Ranked Flex / Solo (10 bans, then B RR BB RR BB R) vs tournament draft (6 bans first)."""
    data, team = synthetic_data()
    for event in demo_sessions(DraftEngine(data, team), fmt, "red"):
        e = split_turns(event) if grouping == "one-per-turn" else event
        assert detect_format(e) is fmt
        assert parse_session(e).state.format is fmt


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


def test_recorded_flex_fixture_contains_no_identities():
    """The committed Ranked Flex recording was sanitized: no names, Riot IDs, PUUIDs, chat or game ids."""
    folder = SAMPLE.parent / "flex-ranked-sample"
    for f in folder.glob("*-session.json"):
        s = json.loads(f.read_text(encoding="utf-8"))
        assert "chatDetails" not in s and s.get("gameId") in (0, 1, None)
        for m in s["myTeam"] + s["theirTeam"]:
            assert m.get("gameName", "").startswith("Player") or not m.get("gameName")
            assert (m.get("puuid") or "puuid-").startswith("puuid-")


def test_recorded_flex_skipped_bans_and_hidden_enemy_bans():
    folder = SAMPLE.parent / "flex-ranked-sample"
    events = sorted(folder.glob("*-session.json"))
    banning = parse_session(json.loads(events[9].read_text(encoding="utf-8")))  # mid ban phase
    assert banning.ally_action == "ban" and banning.state.format is STANDARD_RANKED
    ours = [c for s, c in zip(banning.state.format.steps, banning.state.actions) if s.side == "blue"]
    assert ours and all(c > 0 for c in ours)  # every completed ban of ours already counts
    final = parse_session(json.loads(events[-1].read_text(encoding="utf-8"))).state
    assert -1 not in final.actions and final.bans("red") == [35, 122]  # three enemy bans skipped
