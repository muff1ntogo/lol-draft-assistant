"""docs/roster-profiles.md, Stage B: proposal thresholds and onboarding API."""
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.pools import set_players
from draftassist.db.models import Champion, Player, PlayerGame, PoolEntry
from draftassist.learning.pool_proposal import Game, Mastery, proposal

NOW = 1_800_000_000_000
DAY = 86_400_000
SHARES = {1: {"top": 0.5}, 2: {"top": 0.5}, 3: {"top": 0.5}, 4: {"top": 0.05}, 5: {"top": 0.5}}


def games(champ, n, wins=None, days=1):
    return [Game(champ, "top", i < (wins if wins is not None else n), NOW - days * DAY - i) for i in range(n)]


def test_proposal_uses_games_before_mastery_and_obeys_each_tier_rule():
    rows = proposal(games(1, 15, 8) + games(2, 8, 4) + games(3, 5) + games(4, 1, days=2) + games(5, 1, days=30),
                    [], "top", SHARES, NOW)
    assert [(r["champ_id"], r["tier"], r["source"]) for r in rows] == [
        (1, "main", "games"), (2, "main", "games"), (3, "comfortable", "games"), (4, "learning", "games")]


def test_proposal_mastery_fills_only_to_five_never_main_and_has_role_recency_and_cap():
    rows = proposal(games(1, 5), [Mastery(2, 250_000, NOW), Mastery(3, 60_000, NOW), Mastery(4, 500_000, NOW),
                                  Mastery(5, 60_000, NOW - 181 * DAY)], "top", SHARES, NOW)
    assert [(r["champ_id"], r["tier"], r["source"]) for r in rows] == [
        (1, "comfortable", "games"), (2, "comfortable", "mastery"), (3, "learning", "mastery")]
    many = proposal([], [Mastery(i, 60_000, NOW) for i in range(20)], "top",
                    {i: {"top": 1.0} for i in range(20)}, NOW)
    assert len(many) == 5 and all(r["tier"] != "main" for r in many)


class FakeRiot:
    def account(self, riot_id):
        return {"puuid": "api-puuid", "gameName": "Canonical", "tagLine": "EUW"} if riot_id == "Name#EUW" else None

    def summoner(self, puuid): return {"summonerLevel": 42}
    def ranks(self, puuid): return [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"}]
    def match_ids(self, *args, **kwargs): return ["EUW1_1", "EUW1_2"]
    def match(self, match_id):
        return {"info": {"gameDuration": 1200, "gameStartTimestamp": NOW - DAY, "participants": [
            {"puuid": "api-puuid", "championId": 1 if match_id.endswith("1") else 2,
             "teamPosition": "TOP", "win": True}]}}
    def mastery_all(self, puuid): return [{"championId": 3, "championPoints": 60_000, "lastPlayTime": NOW}]
    def close(self): pass


@pytest.fixture
def db(engine, session):
    for champ_id in (1, 2, 3):
        session.add(Champion(champ_id=champ_id, key=f"C{champ_id}", name=f"Champ {champ_id}", patch="16.21.1"))
    session.commit()
    return engine


def test_onboarding_api_flow_with_fake_riot(make_services, db, monkeypatch):
    svc = make_services(db=db)
    monkeypatch.setattr(svc, "make_riot", lambda: lambda: FakeRiot())
    monkeypatch.setattr(svc.live.engine, "_enemy_role_shares", lambda: {1: {"top": 1.0}, 2: {"top": 1.0}, 3: {"top": 1.0}})
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        lookup = client.post("/api/players/lookup", json={"riot_id": "Name#EUW"}).json()
        assert lookup == {"found": True, "riot_id": "Canonical#EUW", "level": 42,
                          "rank": [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD"}]}
        job = client.post("/api/players/onboard", json={"riot_id": "Name#EUW", "role": "top"}).json()["job"]
        for _ in range(50):
            status = client.get(f"/api/players/onboard/{job}").json()
            if status["status"] != "reading": break
            time.sleep(.01)
        assert status["status"] == "done" and status["games_read"] == 2
        assert [r["champ_id"] for r in status["proposal"]] == [1, 2, 3]
        created = client.post("/api/players", json={"riot_id": "Canonical#EUW", "role": "top", "active": True,
                                                     "pool": [{"champ_id": r["champ_id"], "role": "top", "tier": r["tier"]}
                                                              for r in status["proposal"]]})
        assert created.status_code == 200
    with Session(db) as s:
        player = s.exec(select(Player).where(Player.riot_id == "Canonical#EUW")).one()
        assert player.api_puuid == "api-puuid" and len(s.exec(select(PoolEntry).where(PoolEntry.puuid == player.puuid)).all()) == 3
        assert len(s.exec(select(PlayerGame).where(PlayerGame.puuid == player.puuid)).all()) == 2


def test_onboarding_without_a_key_returns_empty_lookup_and_refuses_a_job(make_services, db):
    with TestClient(create_app(make_services(db=db), stats_refresh_s=0)) as client:
        assert client.post("/api/players/lookup", json={"riot_id": "Name#EUW"}).json() == {"no_key": True}
        job = client.post("/api/players/onboard", json={"riot_id": "Name#EUW", "role": "top"}).json()["job"]
        assert client.get(f"/api/players/onboard/{job}").json() == {
            "status": "done", "games_read": 0, "games_total": 0, "proposal": [], "also_plays": [], "error": None}