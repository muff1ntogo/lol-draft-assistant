"""docs/draft-vs-execution.md, decision 20: Live shows the calibrated P(win) at lock-in, as History's P0 reads it."""
import json

import pytest
from sqlmodel import Session

from draftassist.api.main import load_draft_scale
from draftassist.db.models import ExecutionFit
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.mathx import sigmoid
from draftassist.lcu.demo import demo_sessions


def locked_view(svc, scale):
    svc.live.draft_scale = scale
    events = demo_sessions(svc.engine, TOURNAMENT, "blue")
    return svc.live.view(events[-1])


def test_lock_in_shows_the_calibrated_win_prob(make_services):
    svc = make_services()
    view = locked_view(svc, 0.21)
    center = view["center"]
    assert center["mode"] == "locked"
    ours = {s["role"]: s["champ"]["id"] for s in view["our_slots"]}
    theirs = {s["role"]: s["champ"]["id"] for s in view["their_slots"]}
    engine = svc.engine
    expected = sigmoid(0.21 * (engine.draft_logodds(ours, theirs) + engine.draft_comfort(ours)))
    assert center["calibrated_win_prob"] == pytest.approx(expected, abs=1e-4)
    assert center["win_prob"] is not None  # the raw model number stays, for comparison


def test_a_scale_of_one_reads_the_draft_model_as_it_is(make_services):
    svc = make_services()
    small, one = locked_view(svc, 0.21)["center"], locked_view(svc, 1.0)["center"]
    # the same log-odds, shrunk by the scale
    assert (small["calibrated_win_prob"] - 0.5) * (one["calibrated_win_prob"] - 0.5) >= 0
    assert abs(small["calibrated_win_prob"] - 0.5) <= abs(one["calibrated_win_prob"] - 0.5)


def test_without_a_fit_there_is_no_calibrated_number(make_services):
    center = locked_view(make_services(), None)["center"]
    assert center["calibrated_win_prob"] is None and center["win_prob"] is not None


def test_the_scale_comes_from_the_latest_fit(engine):
    assert load_draft_scale(engine) is None
    with Session(engine) as s:
        s.add(ExecutionFit(window="16.21", body=json.dumps({"draft_scale": 0.3})))
        s.add(ExecutionFit(window="16.22", body=json.dumps({"draft_scale": 0.21})))
        s.commit()
    assert load_draft_scale(engine) == pytest.approx(0.21)


def test_services_load_the_scale(make_services, engine):
    with Session(engine) as s:
        s.add(ExecutionFit(window="16.22", body=json.dumps({"draft_scale": 0.21})))
        s.commit()
    svc = make_services(db=engine)
    svc.reload()
    assert svc.live.draft_scale == pytest.approx(0.21)
