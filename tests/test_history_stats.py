"""docs/history-charts.md: History's overview band."""
import json

import pytest
from conftest import REAL_NAMES
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.history_stats import wilson
from draftassist.api.main import create_app
from draftassist.db.models import Champion, Draft, GameExecution, OwnGameParticipant
from draftassist.engine.formats import TOURNAMENT
from draftassist.lcu.demo import demo_sessions
from test_recap import play


@pytest.fixture
def svc(make_services, engine, session):
    for cid, name in REAL_NAMES.items():
        session.add(Champion(champ_id=cid, key=name.replace("'", "").replace(" ", ""), name=name, patch="16.21.1"))
    session.commit()
    return make_services(db=engine)


def stats(svc, **params):
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        return client.get("/api/history/stats", params=params).json()


def test_wilson_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(3, 5)
    assert lo < 0.6 < hi and hi - lo > 0.5  # 3 of 5 is weak evidence
    lo, hi = wilson(60, 100)
    assert 0.49 < lo < 0.6 < hi < 0.7


def test_empty_history(svc):
    body = stats(svc)
    assert body["games"] == 0 and body["trend"] == [] and body["players"] == []


def test_overview_from_logged_games(svc, engine):
    ids = []
    for i, (side, won) in enumerate([("blue", True), ("red", False), ("blue", True)]):
        gid = 7_300_000_000 + i
        ids.append(play(svc, demo_sessions(svc.engine, TOURNAMENT, side, game_id=gid), gid, side, won))
    with Session(engine) as s:
        s.add(GameExecution(draft_id=ids[0], window="16.21", p0=0.5, draft=0.04, early=0.1, conversion=0.36,
                            verdict="won lane"))
        d = s.get(Draft, ids[0])
        d.players_json = json.dumps({"mid": "Midder"})
        s.add(d)
        s.commit()
    body = stats(svc)
    assert body["games"] == 3 and body["overall"]["wins"] == 2 and body["overall"]["ci"]
    assert [t["result"] for t in body["trend"]] == ["win", "loss", "win"]
    assert body["trend"][-1]["rolling_wr"] == pytest.approx(2 / 3, abs=1e-3)
    assert sum(b["games"] for b in body["calibration"]) == 3
    assert {s["name"] for s in body["sides"]} == {"blue", "red"}
    assert body["execution"] == [{"game": 1, "draft_id": ids[0], "result": "win", "verdict": "won lane",
                                  "draft": 4.0, "early": 10.0, "conversion": 36.0}]
    players = {p["player"] for p in body["players"]}
    assert "Midder" in players and len(body["players"]) >= 5
    assert sum(r["games"] for p in body["players"] for r in p["champions"]) == 15
    assert body["bans"] and all(b["games"] >= 1 for b in body["bans"])
    assert sum(f["games"] for f in body["followed"]) == 3
    # side filter
    assert stats(svc, side="red")["games"] == 1


def test_lane_gold_feeds_the_player_grid(svc, engine):
    gid = 7_300_000_100
    did = play(svc, demo_sessions(svc.engine, TOURNAMENT, "blue", game_id=gid), gid, "blue", True)
    body = stats(svc)
    champ = body["players"][0]["champions"][0]["champ"]["id"]
    with Session(engine) as s:
        s.add(OwnGameParticipant(draft_id=did, champ_id=champ, match_id="EUW1_1", team_id=100, lane_gold_15=420.0))
        s.commit()
    row = stats(svc)["players"][0]["champions"][0]
    assert row["gold15"] == 420 and row["gold_games"] == 1


def test_timeframe_keeps_the_last_games(svc):
    results = [True, False, False, True, True, True, False]
    for i, won in enumerate(results):
        gid = 7_400_000_000 + i
        side = "blue" if i % 2 == 0 else "red"
        play(svc, demo_sessions(svc.engine, TOURNAMENT, side, game_id=gid), gid, side, won)
    every = stats(svc)
    assert every["games"] == every["total_games"] == 7 and every["last"] is None and every["rolling"] == 10
    recent = stats(svc, last=3)
    assert recent["games"] == 3 and recent["total_games"] == 7 and recent["overall"]["wins"] == 2
    assert recent["rolling"] == 5  # a short timeframe smooths over fewer games
    # the trend keeps its game numbers, and its rolling line reads games from before the timeframe
    assert [t["game"] for t in recent["trend"]] == [5, 6, 7]
    assert recent["trend"][0]["rolling_wr"] == pytest.approx(3 / 5, abs=1e-3)
    assert sum(b["games"] for b in recent["calibration"]) <= 3
    assert stats(svc, last=0)["games"] == 7  # 0 means every game
