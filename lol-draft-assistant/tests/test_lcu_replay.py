"""LCU replay: recorded champ select sessions run through the full pipeline (parser -> engine ->
guards -> live view) with no violations at any event, and the draft only ever grows.

Sources: committed fixtures (fixtures/lcu/<name>/NNNN-session.json, optional expected.json) and
local day-one recordings (fixtures/lcu/recorded/, git-ignored). Re-record after client patches.
"""
import json
from pathlib import Path

import pytest

from draftassist.lcu.parser import parse_session

LCU_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lcu"
SEQUENCES = sorted({p.parent for p in LCU_FIXTURES.glob("**/*-session.json")})


@pytest.mark.parametrize("folder", SEQUENCES, ids=lambda p: str(p.relative_to(LCU_FIXTURES)))
def test_replay_sequence(folder, make_services):
    svc = make_services()
    events = sorted(folder.glob("*-session.json"))
    expected_file = folder / "expected.json"
    expected = json.loads(expected_file.read_text(encoding="utf-8")) if expected_file.is_file() else None
    prev = ()
    state = None
    for i, path in enumerate(events):
        session = json.loads(path.read_text(encoding="utf-8"))
        state = parse_session(session).state
        # Never goes backwards: nothing locked disappears (the two picks of a turn may lock in
        # either order, and hidden ranked bans are filled in when revealed).
        assert len(state.actions) >= len(prev), f"{path.name}: draft got shorter"
        assert {c for c in prev if c} <= set(state.actions), f"{path.name}: a locked champion disappeared"
        prev = state.actions
        if expected is not None and i < len(expected.get("states", [])):
            assert list(state.actions) == expected["states"][i]["actions"]
        view = svc.live.view(session)  # strict guards: raises on any violation
        assert view["status"] == "champ_select", f"{path.name}: {view}"
    if expected is not None and "final_actions" in expected:
        assert list(state.actions) == expected["final_actions"]
        assert state.format.name == expected["format"] and state.our_side == expected["our_side"]


def test_there_is_at_least_one_sequence():
    assert SEQUENCES, "no LCU fixtures found"
