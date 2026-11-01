"""Test 12 from the ban-guard task: replay recorded champ select sessions through the full pipeline
(parser -> engine -> guards) with no violations at any event.

Needs the LCU session parser (Phase 2) and recorded sessions in fixtures/ (day-one dump).
"""
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_replay_recorded_sessions_without_violations():
    sessions = sorted(FIXTURES.glob("**/*.json"))
    if not sessions:
        pytest.skip("no recorded LCU sessions in fixtures/ yet (day-one dump)")
    pytest.skip("LCU session parser lands in Phase 2; replay is wired up there")
