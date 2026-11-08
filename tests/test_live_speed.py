"""Live speed and no-crash (docs/live-speed.md): coalesced events, board first, panels that fail on their own,
the last good suggestions kept (stale), and a time budget per event over every recorded and simulated draft."""
import json
import threading
import time
from pathlib import Path

import pytest

import draftassist.api.live as live_mod
from draftassist.engine.formats import get_format
from draftassist.lcu.demo import demo_sessions

LCU_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lcu"
BUDGET_MS = 400  # per event, generous for a slow test machine; the app logs anything over SLOW_BUILD_MS


class RecordingHub(live_mod.LiveHub):
    def __init__(self):
        super().__init__()
        self.sent: list[dict] = []

    def publish_threadsafe(self, payload):
        self.latest = payload
        self.sent.append(payload)


@pytest.fixture
def svc(make_services):
    s = make_services()
    s.live.hub = RecordingHub()
    return s


def _sessions(svc, fmt="tournament"):
    return demo_sessions(svc.live.engine, get_format(fmt), "blue", game_id=7)


def _our_pick_index(svc, sessions):
    return next(i for i, s in enumerate(sessions) if svc.live.view(s)["center"]["mode"] == "our_pick")


def test_submit_keeps_only_the_newest_waiting_session(svc, monkeypatch):
    sessions = _sessions(svc)
    real = live_mod.build_live_view
    gate = threading.Event()

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(live_mod, "build_live_view", slow)
    for s in sessions[:6]:
        svc.live.submit(s)
    gate.set()
    assert svc.live.drain(10)
    assert svc.live.dropped >= 4  # the first is building; the 2nd..5th were replaced by the 6th
    last = svc.live.hub.sent[-1]
    assert last["step_index"] == svc.live.view(sessions[5])["step_index"]


def test_idle_phase_drops_a_waiting_session(svc):
    live = svc.live
    with live._cv:  # no worker: queue directly
        live._next = _sessions(svc)[3]
    live.handle_phase("Lobby", None)
    assert live._next is live_mod._NOTHING
    assert svc.live.hub.sent[-1]["status"] == "waiting"


def test_a_failing_panel_hides_only_itself(svc, monkeypatch):
    monkeypatch.delenv("DRAFTASSIST_STRICT")

    def boom(*a, **k):
        raise RuntimeError("comp broke")

    monkeypatch.setattr(live_mod, "team_comp", boom)
    sessions = _sessions(svc)
    view = svc.live.view(sessions[_our_pick_index(svc, sessions)])
    assert view["status"] == "champ_select"
    assert view["comp"] is None and "comp panels" in view["panel_errors"]
    assert view["center"]["mode"] == "our_pick" and view["center"]["cards"]


def test_failed_suggestions_fall_back_to_the_last_good_ones_marked_stale(svc, monkeypatch, tmp_path):
    monkeypatch.delenv("DRAFTASSIST_STRICT")
    sessions = _sessions(svc)
    i = _our_pick_index(svc, sessions)
    good = svc.live.handle_session(sessions[i])
    assert good["center_stale"] is None

    def boom(*a, **k):
        raise RuntimeError("engine broke")

    monkeypatch.setattr(live_mod, "_center", boom)
    observed = []
    svc.live.logger = type("L", (), {"observe": lambda self, live, p: observed.append(p)})()
    after = svc.live.handle_session(sessions[i + 1])
    assert "suggestions" in after["panel_errors"]
    assert after["center"] == good["center"]
    assert after["center_stale"]["phase"] == good["phase"]
    assert observed[-1]["center"] is None  # the draft log never records a stale centre as this step's


def test_failed_suggestions_without_a_good_one_say_unavailable(svc, monkeypatch):
    monkeypatch.delenv("DRAFTASSIST_STRICT")
    monkeypatch.setattr(live_mod, "_center", lambda *a: 1 / 0)
    view = svc.live.handle_session(_sessions(svc)[0])
    assert view["center"]["mode"] == "unavailable" and view["our_slots"]


def test_stale_centre_never_crosses_drafts(svc, monkeypatch):
    monkeypatch.delenv("DRAFTASSIST_STRICT")
    svc.live.handle_session(_sessions(svc)[2])
    other = demo_sessions(svc.live.engine, get_format("tournament"), "red", game_id=8)
    monkeypatch.setattr(live_mod, "_center", lambda *a: 1 / 0)
    view = svc.live.handle_session(other[2])
    assert view["center"]["mode"] == "unavailable"


def test_a_slow_build_sends_the_board_first(svc):
    sessions = _sessions(svc)
    svc.live.handle_session(sessions[0])
    svc.live.last_build_ms = live_mod.BOARD_FIRST_MS + 1
    n = len(svc.live.hub.sent)
    svc.live.handle_session(sessions[1])
    board, full = svc.live.hub.sent[n:]
    assert board["pending"] is True and board["center_stale"]["updating"] is True
    assert board["comp"] is None and board["our_bans"] == full["our_bans"]
    assert full["pending"] is False and full["center_stale"] is None


def test_build_time_is_reported(svc):
    view = svc.live.handle_session(_sessions(svc)[0])
    assert view["build_ms"] >= 0 and svc.live.last_build_ms is not None


SEQUENCES = sorted({p.parent for p in LCU_FIXTURES.glob("**/*-session.json")})


@pytest.mark.parametrize("source", [*[str(p) for p in SEQUENCES], "demo:tournament", "demo:ranked"])
def test_every_event_builds_within_budget(svc, source):
    if source.startswith("demo:"):
        sessions = _sessions(svc, source.split(":")[1])
    else:
        sessions = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(source).glob("*-session.json"))]
    slow = []
    for s in sessions:
        t = time.perf_counter()
        view = svc.live.view(s)  # strict: any panel error raises
        ms = (time.perf_counter() - t) * 1000
        assert view["status"] == "champ_select" and not view["panel_errors"]
        if ms > BUDGET_MS:
            slow.append(round(ms))
    assert not slow, f"events over {BUDGET_MS} ms: {slow}"
