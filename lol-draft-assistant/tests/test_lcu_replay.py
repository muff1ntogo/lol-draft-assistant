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
    expected = json.loads(expected_file.read_text(encoding="utf-8"))["states"] if expected_file.is_file() else None
    prev = ()
    for i, path in enumerate(events):
        session = json.loads(path.read_text(encoding="utf-8"))
        state = parse_session(session).state
        assert state.actions[: len(prev)] == prev, f"{path.name}: draft went backwards"
        prev = state.actions
        if expected is not None and i < len(expected):
            assert list(state.actions) == expected[i]["actions"]
        view = svc.live.view(session)  # strict guards: raises on any violation
        assert view["status"] == "champ_select", f"{path.name}: {view}"


def test_there_is_at_least_one_sequence():
    assert SEQUENCES, "no LCU fixtures found"
