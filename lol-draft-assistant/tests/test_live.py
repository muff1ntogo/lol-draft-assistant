import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from draftassist.api.main import create_app
from draftassist.engine.formats import TOURNAMENT
from draftassist.lcu.demo import demo_sessions

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "lcu" / "sample-tournament" / "0001-session.json"


def sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_sample_session_view(make_services):
    view = make_services().live.view(sample())
    assert view["status"] == "champ_select" and view["side"] == "red" and view["turn"] == "us"
    assert view["phase"] == "Pick 1" and view["action"] == "pick" and view["seconds_left"] == 25.0
    assert view["center"]["mode"] == "our_pick" and view["center"]["title"] == "PICK — Top"
    cards = view["center"]["cards"]
    assert 1 <= len(cards) <= 3 and all(c["role"] == "top" for c in cards)
    assert [b["name"] for b in view["their_bans"]] == ["Azir", "Jinx"]
    assert [b["name"] for b in view["our_bans"]] == ["Gnar", "Vi", "Rakan"]
    assert view["our_slots"][0]["hover"]["name"] == "K'Sante" and view["our_slots"][0]["champ"] is None
    assert view["our_slots"][1]["hover"]["name"] == "Sejuani"
    assert view["their_slots"][0]["champ"]["name"] == "Ahri"
    assert view["our_slots"][0]["hover"]["icon"].endswith("/16.21.1/img/champion/KSante.png")


def test_every_demo_event_renders_with_guards_on(make_services):
    svc = make_services()
    modes = set()
    for side in ("blue", "red"):
        for event in demo_sessions(svc.engine, TOURNAMENT, side):
            view = svc.live.view(event)  # strict mode: any guard violation raises
            assert view["status"] == "champ_select", view
            modes.add(view["center"]["mode"])
            unavailable = {c["id"] for c in view["our_bans"] + view["their_bans"]}
            unavailable |= {s["champ"]["id"] for s in view["our_slots"] + view["their_slots"] if s["champ"]}
            for card in view["center"].get("cards", []):
                assert card["champ"]["id"] not in unavailable
    assert modes == {"our_ban", "our_pick", "enemy_turn", "locked"}


def test_hovered_pick_card_is_highlighted(make_services):
    svc = make_services()
    events = demo_sessions(svc.engine, TOURNAMENT, "blue")
    hover_views = [svc.live.view(e) for e in events if any(s["hover"] for s in svc.live.view(e)["our_slots"])]
    assert hover_views
    for view in hover_views:
        if view["center"]["mode"] == "our_pick":
            highlighted = [c for c in view["center"]["cards"] if c["highlighted"]]
            hover_card = view["center"]["hover_card"]
            assert highlighted or (hover_card and hover_card["highlighted"])


def test_lock_in_view_has_win_prob_and_two_threats(make_services):
    svc = make_services()
    final = demo_sessions(svc.engine, TOURNAMENT, "red")[-1]
    view = svc.live.view(final)
    assert view["phase"] == "Locked in" and view["turn"] is None
    assert 0 < view["center"]["win_prob"] < 1
    assert len(view["center"]["threats"]) == 2 and all(t["reason"] for t in view["center"]["threats"])


def test_session_end_keeps_lock_in_until_lobby(make_services):
    svc = make_services()
    svc.live.handle_session(demo_sessions(svc.engine, TOURNAMENT, "blue")[-1])
    assert svc.hub.latest["phase"] == "Locked in"
    svc.live.handle_session(None)  # game loading
    assert svc.hub.latest["phase"] == "Locked in"
    svc.live.handle_phase("Lobby", None)
    assert svc.hub.latest["status"] == "waiting"


def test_dodge_clears_screen(make_services):
    svc = make_services()
    svc.live.handle_session(sample())
    svc.live.handle_session(None)
    assert svc.hub.latest["status"] == "waiting"


def test_parse_error_shows_error_not_crash(make_services):
    bad = sample()
    del bad["myTeam"]
    view = make_services().live.view(bad)
    assert view["status"] == "error" and "myTeam" in view["message"]


def test_websocket_push_within_one_second(make_services):
    svc = make_services()
    events = demo_sessions(svc.engine, TOURNAMENT, "blue")
    with TestClient(create_app(svc)) as client, client.websocket_connect("/ws/live") as ws:
        assert ws.receive_json()["status"] == "waiting"
        worst = 0.0
        for event in events:
            t0 = time.perf_counter()
            svc.live.handle_session(event)  # what the connector thread does on each client event
            payload = ws.receive_json()
            worst = max(worst, time.perf_counter() - t0)
            assert payload["status"] == "champ_select"
        assert worst < 1.0, f"slowest event-to-screen push took {worst * 1000:.0f} ms"
        assert client.get("/api/live").json()["phase"] == "Locked in"
