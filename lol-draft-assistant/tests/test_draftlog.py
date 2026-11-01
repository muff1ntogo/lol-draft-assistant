"""Phase 4: every draft and its result is logged, and shows up in History with the correct
result, side and every action."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.draftlog import parse_eog
from draftassist.api.main import create_app
from draftassist.data.postgame import verify_draft
from draftassist.data.riot import RateLimiter, RiotClient
from draftassist.db.models import Champion, Draft, DraftAction
from draftassist.engine.formats import TOURNAMENT
from draftassist.lcu.demo import demo_sessions

from conftest import REAL_NAMES


@pytest.fixture
def svc(make_services, engine, session):
    for cid, name in REAL_NAMES.items():
        session.add(Champion(champ_id=cid, key=name.replace("'", "").replace(" ", ""), name=name, patch="16.21.1"))
    session.commit()
    return make_services(db=engine)


def eog(game_id, side, won, length=1800):
    team = 100 if side == "blue" else 200
    return {"gameId": game_id, "gameLength": length,
            "teams": [{"teamId": team, "isPlayerTeam": True, "isWinningTeam": won},
                      {"teamId": 300 - team, "isPlayerTeam": False, "isWinningTeam": not won}]}


def play(svc, side, game_id, won, start_phase=True, length=1800):
    events = demo_sessions(svc.engine, TOURNAMENT, side, game_id=game_id)
    svc.live.handle_phase("ChampSelect", None)
    for e in events:
        svc.live.handle_session(e)
    if start_phase:
        svc.live.handle_phase("GameStart", None)
        svc.live.handle_session(None)
        svc.live.handle_phase("InProgress", None)
    svc.live.handle_phase("EndOfGame", None)
    draft_id = svc.live.handle_end_of_game(eog(game_id, side, won, length))
    svc.live.handle_phase("Lobby", None)
    return events, draft_id


def final_actions(events):
    done = sorted((a for t in events[-1]["actions"] for a in t if a["completed"]), key=lambda a: a["id"])
    return [a["championId"] for a in done]


@pytest.mark.parametrize("side,won", [("blue", True), ("red", False)])
def test_game_appears_in_history_with_result_side_and_every_action(svc, side, won):
    events, draft_id = play(svc, side, 7_100_000_001, won)
    assert draft_id is not None
    with TestClient(create_app(svc)) as client:
        (game,) = client.get("/api/history").json()
    assert game["game_id"] == 7_100_000_001 and game["side"] == side
    assert game["result"] == ("win" if won else "loss") and game["result_source"] == "lcu"
    assert game["format"] == "tournament" and 0 < game["pred_win_prob"] < 1
    actions = game["actions"]
    assert [a["seq"] for a in actions] == list(range(1, 21))
    assert [a["champ"]["id"] if a["champ"] else 0 for a in actions] == final_actions(events)
    assert [(a["side"], a["type"]) for a in actions] == [(s.side, s.kind) for s in TOURNAMENT.steps]
    # the demo follows our top suggestion, and the enemy picks our most likely prediction
    assert game["summary"]["our_turns"] == 10 and game["summary"]["followed_top"] == 10
    assert game["summary"]["enemy_picks_predicted"] == 5 and game["summary"]["enemy_picks_in_our_top3"] == 5
    ours = [a for a in actions if a["ours"]]
    assert all(a["shown_kind"] == "suggested" and a["our_rank"] == 1 and a["shown"] for a in ours)


def test_dodge_is_not_logged(svc, engine):
    events = demo_sessions(svc.engine, TOURNAMENT, "blue", game_id=7_100_000_002)
    for e in events[:9]:
        svc.live.handle_session(e)
    svc.live.handle_session(None)
    svc.live.handle_phase("Lobby", None)
    with Session(engine) as s:
        assert s.exec(select(Draft)).all() == []


def test_result_still_logged_if_game_start_was_missed(svc, engine):
    _, draft_id = play(svc, "red", 7_100_000_003, True, start_phase=False)
    with Session(engine) as s:
        d = s.get(Draft, draft_id)
        assert d.result == "win" and d.side == "red"
        assert len(s.exec(select(DraftAction).where(DraftAction.draft_id == draft_id)).all()) == 20


def test_two_games_two_drafts_and_remakes(svc, engine):
    play(svc, "blue", 7_100_000_004, True)
    play(svc, "red", 7_100_000_005, False, length=200)
    with Session(engine) as s:
        drafts = {d.game_id: d for d in s.exec(select(Draft)).all()}
    assert drafts[7_100_000_004].result == "win" and drafts[7_100_000_005].result == "remake"


def test_suggestions_are_stored_with_each_action(svc, engine):
    _, draft_id = play(svc, "blue", 7_100_000_006, True)
    with Session(engine) as s:
        first = s.exec(select(DraftAction).where(DraftAction.draft_id == draft_id, DraftAction.seq == 1)).one()
        enemy = s.exec(select(DraftAction).where(DraftAction.draft_id == draft_id, DraftAction.seq == 8)).one()
    shown = json.loads(first.suggestions_json)
    assert shown["kind"] == "suggested" and len(shown["items"]) == 3 and shown["items"][0]["reason"]
    assert first.our_rank == 1 and first.champ_id == shown["items"][0]["champ_id"]
    assert json.loads(enemy.suggestions_json)["kind"] == "predicted"


def test_parse_eog_defensively():
    assert parse_eog(None) == (None, None, None)
    assert parse_eog({"gameId": 5, "teams": [{"teamId": 200, "isPlayerTeam": True, "isWinningTeam": False}]}) == (5, "loss", "red")
    assert parse_eog({"gameId": 5, "teams": []}) == (5, None, None)


# ---------- match-v5 verification ----------
def riot_with_match(tmp_path, match):
    def handler(req):
        if match is not None and req.url.path.endswith(match["metadata"]["matchId"]):
            return httpx.Response(200, json=match)
        return httpx.Response(404)

    return RiotClient("RGAPI-test", tmp_path, limiter=RateLimiter(sleep=lambda s: None),
                      transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_match_v5_verifies_or_corrects(svc, engine, tmp_path):
    events, draft_id = play(svc, "blue", 7_100_000_007, True)
    acts = final_actions(events)
    blue_picks = [acts[i] for i, s in enumerate(TOURNAMENT.steps) if s.kind == "pick" and s.side == "blue"]
    red_picks = [acts[i] for i, s in enumerate(TOURNAMENT.steps) if s.kind == "pick" and s.side == "red"]
    participants = ([{"championId": c, "teamId": 100, "win": False, "puuid": f"b{c}"} for c in blue_picks]
                    + [{"championId": c, "teamId": 200, "win": True, "puuid": f"r{c}"} for c in red_picks])
    match = {"metadata": {"matchId": "EUW1_7100000007"}, "info": {"queueId": 440, "participants": participants}}
    assert verify_draft(engine, riot_with_match(tmp_path, match), draft_id) == "corrected"  # client said win
    with Session(engine) as s:
        d = s.get(Draft, draft_id)
        assert (d.result, d.side, d.match_verified, d.result_source, d.queue_id) == ("loss", "blue", True, "match-v5", 440)
    assert verify_draft(engine, riot_with_match(tmp_path / "x", None), draft_id) == "not-found"


# ---------- API ----------
def test_pools_api_import_and_errors(svc):
    with TestClient(create_app(svc)) as client:
        bad = client.post("/api/pools/import", json={"text": "TOP\nKsantee: main"})
        assert bad.status_code == 422 and "did you mean K'Sante" in bad.json()["detail"]["errors"][0]
        assert client.get("/api/pools").json()["using_placeholder_pools"] is True
        ok = client.post("/api/pools/import", json={"text": "TOP\nK'Sante: main\nGnar: learning\n\nMID\nAzir: main"})
        assert ok.status_code == 200 and ok.json() == {"imported": {"top": 2, "mid": 1}}
        pools = client.get("/api/pools").json()
        assert [e["champ"]["name"] for e in pools["pools"]["top"]] == ["K'Sante", "Gnar"]
        assert pools["using_placeholder_pools"] is False
        # the live engine now uses the imported pools
        assert svc.engine.team.entry(897, "top").tier == "main"
        players = client.put("/api/players", json={"players": [{"role": "top", "riot_id": "Me#EUW", "rank": "Gold I"}]})
        assert players.status_code == 200 and players.json()[0]["riot_id"] == "Me#EUW"
        assert client.put("/api/players", json={"players": [{"role": "top", "riot_id": "nope"}]}).status_code == 422
