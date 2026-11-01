"""Phase 1 acceptance: a simulated 20-step draft completes in both formats, no banned or picked
champion is ever suggested, and each suggestion set returns in under 200 ms."""
import pytest

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.model import ROLES
from draftassist.engine.simulate import run_draft, synthetic_data
from draftassist.engine.state import DraftState

BUDGET_SECONDS = 0.200


@pytest.fixture(scope="module")
def synthetic():
    return synthetic_data(seed=7)


@pytest.mark.parametrize("fmt", [TOURNAMENT, STANDARD_RANKED], ids=lambda f: f.name)
@pytest.mark.parametrize("side", ["blue", "red"])
def test_simulated_draft(synthetic, fmt, side):
    data, team = synthetic
    engine = DraftEngine(data, team)
    final, records = run_draft(engine, fmt, side)

    assert final.is_complete and len(records) == 20
    assert len(final.our_picks) == len(final.their_picks) == 5
    ev = engine.evaluate(final)
    assert sorted(ev.our_roles.values()) == sorted(ROLES)
    assert sorted(ev.their_roles.values()) == sorted(ROLES)

    our_turns = [r for r in records if r.step.side == side]
    assert len(our_turns) == 10 and all(r.suggestions for r in our_turns)

    for rec in records:
        before = DraftState(fmt, side, final.actions[: rec.step.index])
        unavailable = before.unavailable()
        filled = set(engine.evaluate(before).our_roles.values())
        their_filled = set(engine.evaluate(before).their_roles.values())
        for sug in rec.suggestions:
            assert sug.champ_id not in unavailable, f"step {rec.step.index + 1} suggested unavailable {sug.name}"
            assert sug.kind == rec.step.kind
            assert sug.reason
            if sug.kind == "pick":
                assert sug.role not in filled
            elif rec.step.phase == 2:
                assert sug.enemy_prob > 0 and sug.role not in their_filled

    slowest = max(r.seconds for r in our_turns)
    assert slowest < BUDGET_SECONDS, f"slowest suggestion set took {slowest * 1000:.0f} ms"


def test_simulation_is_deterministic(synthetic):
    data, team = synthetic
    a, _ = run_draft(DraftEngine(data, team), TOURNAMENT, "blue")
    b, _ = run_draft(DraftEngine(data, team), TOURNAMENT, "blue")
    assert a.actions == b.actions
