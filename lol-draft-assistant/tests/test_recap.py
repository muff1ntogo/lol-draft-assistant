"""docs/history-broadcast-ui.md: History list and the broadcast-style recap payload."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.api.recap import lane_matchup
from draftassist.db.models import Champion, DraftAction, Matchup
from draftassist.engine.formats import TOURNAMENT
from draftassist.lcu.demo import demo_sessions

from conftest import REAL_NAMES

FLEX = Path(__file__).resolve().parents[1] / "fixtures" / "lcu" / "flex-ranked-sample"


@pytest.fixture
def svc(make_services, engine, session):
    for cid, name in REAL_NAMES.items():
        session.add(Champion(champ_id=cid, key=name.replace("'", "").replace(" ", ""), name=name, patch="16.21.1"))
    session.commit()
    return make_services(db=engine)


def eog(game_id, side, won):
    team = 100 if side == "blue" else 200
    return {"gameId": game_id, "gameLength": 1835,
            "teams": [{"teamId": team, "isPlayerTeam": True, "isWinningTeam": won},
                      {"teamId": 300 - team, "isPlayerTeam": False, "isWinningTeam": not won}]}


def play(svc, events, game_id, side, won):
    svc.live.handle_phase("ChampSelect", None)
    for e in events:
        svc.live.handle_session(e)
    svc.live.handle_phase("InProgress", None)
    svc.live.handle_phase("EndOfGame", None)
    draft_id = svc.live.handle_end_of_game(eog(game_id, side, won))
    svc.live.handle_phase("Lobby", None)
    return draft_id


def get_recap(svc, draft_id):
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        return client.get(f"/api/history/{draft_id}").json(), client.get("/api/history").json()


# 1. Tournament draft --------------------------------------------------------------------------
def test_tournament_recap(svc):
    draft_id = play(svc, demo_sessions(svc.engine, TOURNAMENT, "blue", game_id=7_200_000_001), 7_200_000_001, "blue", True)
    recap, listing = get_recap(svc, draft_id)
    assert [b["phase"] for b in recap["bans"]] == [1, 2]
    assert [(len(b["blue"]), len(b["red"])) for b in recap["bans"]] == [(3, 3), (2, 2)]  # 6 + 4
    tags = [p[s]["tag"] for p in recap["picks"] for s in ("blue", "red")]
    assert sorted(tags) == sorted(["B1", "B2", "B3", "B4", "B5", "R1", "R2", "R3", "R4", "R5"])
    assert [p["role"] for p in recap["picks"]] == ["top", "jungle", "mid", "adc", "support"]
    assert len([p for p in recap["picks"] if p["matchup"] is not None]) == 5
    assert len(recap["timeline"]) == 20 and recap["timeline"][0]["label"] == "ban"
    assert recap["review"]["biggest_swing"]["step"] in range(1, 21)
    assert recap["header"]["result"] == "win" and recap["header"]["game_length_s"] == 1835
    assert recap["note"] is None  # P(win) per step was stored at draft time
    (row,) = listing["games"]
    assert len(row["our_champs"]) == len(row["their_champs"]) == 5


# 2. Flex -------------------------------------------------------------------------------------
def test_flex_recap_has_one_ban_group(svc):
    events = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(FLEX.glob("*-session.json"))]
    draft_id = play(svc, events, 1, "blue", False)
    recap, listing = get_recap(svc, draft_id)
    assert len(recap["bans"]) == 1 and recap["bans"][0]["label"] == "Bans"
    ban_group = recap["bans"][0]
    assert len(ban_group["blue"]) == len(ban_group["red"]) == 5
    assert sum(1 for c in ban_group["red"] if c is None) == 3  # the enemy skipped three bans: shown as "—"
    assert listing["games"][0]["queue"] == "Standard draft"  # no queue id in the fixture
    assert listing["filters"]["side"] == ["blue", "red"]


# 3. Favourability sign -------------------------------------------------------------------------
def test_matchup_favourability_sign(engine, session):
    session.add(Matchup(patch="16.21", bracket="gold", role="top", champ_a=1, champ_b=2, games=2000, wins_a=1160))
    session.add(Matchup(patch="16.21", bracket="gold", role="mid", champ_a=3, champ_b=4, games=30, wins_a=25))
    session.add(Matchup(patch="16.21", bracket="gold", role="adc", champ_a=5, champ_b=6, games=900, wins_a=452))
    session.commit()
    favoured = lane_matchup(engine, "16.21", "gold", "top", 1, 2)
    assert favoured["delta_pp"] > 0 and favoured["band"] == "ours"
    unfavoured = lane_matchup(engine, "16.21", "gold", "top", 2, 1)  # same lane, other side
    assert unfavoured["delta_pp"] == pytest.approx(-favoured["delta_pp"]) and unfavoured["band"] == "theirs"
    assert lane_matchup(engine, "16.21", "gold", "mid", 3, 4)["band"] == "no-data"  # under 50 games
    assert lane_matchup(engine, "16.21", "gold", "adc", 5, 6)["band"] == "even"  # within 1pp


# 4. Backfill ------------------------------------------------------------------------------------
def test_old_game_gets_win_probs_on_first_view(svc, engine):
    draft_id = play(svc, demo_sessions(svc.engine, TOURNAMENT, "red", game_id=7_200_000_002), 7_200_000_002, "red", True)
    with Session(engine) as s:  # make it look like a game logged before win_prob_after existed
        for a in s.exec(select(DraftAction).where(DraftAction.draft_id == draft_id)).all():
            a.win_prob_after = None
            s.add(a)
        s.commit()
    recap, _ = get_recap(svc, draft_id)
    assert recap["note"] and len(recap["timeline"]) == 20
    with Session(engine) as s:
        stored = [a.win_prob_after for a in s.exec(select(DraftAction).where(DraftAction.draft_id == draft_id))]
    assert len(stored) == 20 and all(v is not None for v in stored)
    again, _ = get_recap(svc, draft_id)
    assert again["note"] == recap["note"]  # still says the timeline was computed later


def test_history_filters(svc):
    play(svc, demo_sessions(svc.engine, TOURNAMENT, "blue", game_id=7_200_000_003), 7_200_000_003, "blue", True)
    play(svc, demo_sessions(svc.engine, TOURNAMENT, "red", game_id=7_200_000_004), 7_200_000_004, "red", False)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert len(client.get("/api/history").json()["games"]) == 2
        assert [g["side"] for g in client.get("/api/history?side=red").json()["games"]] == ["red"]
        assert [g["result"] for g in client.get("/api/history?result=win").json()["games"]] == ["win"]
        assert client.get("/api/history/999").status_code == 404
        assert client.get("/api/art/16.21.1/square/..%2F..%2Fsecret").status_code == 404
