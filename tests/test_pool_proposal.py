"""docs/roster-profiles.md, Stage B: proposal thresholds and onboarding API."""
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.pools import set_players
from draftassist.db.models import Champion, Player, PlayerGame, PoolEntry
from draftassist.learning.pool_proposal import Game, Mastery, proposal

NOW = 1_800_000_000_000
NOW_MS = int(time.time() * 1000)  # the onboarding job reads the clock
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


def test_proposal_thresholds_at_their_edges():
    def tiers(gs, mastery=()):
        return {r["champ_id"]: (r["tier"], r["source"]) for r in proposal(gs, mastery, "top", SHARES, NOW)}

    # main by share: 8 games that are a quarter of their games in the role; 7 are not enough, whatever the share
    assert tiers(games(1, 8, 4) + games(2, 24, 12))[1] == ("main", "games")
    assert tiers(games(1, 7, 7))[1] == ("comfortable", "games")
    assert tiers(games(1, 8, 4) + games(2, 30, 15))[1] == ("comfortable", "games")  # 8 of 38 is under a quarter
    assert tiers(games(1, 15, 2))[1] == ("comfortable", "games")  # 15 games but losing: adjusted win rate under 45%
    # learning: 2-4 games, or one game in the last 14 days; one older game is nothing
    assert tiers(games(1, 4))[1] == ("learning", "games") and tiers(games(1, 2, days=40))[1] == ("learning", "games")
    assert tiers(games(1, 1, days=13))[1] == ("learning", "games") and 1 not in tiers(games(1, 1, days=15))
    # games in another role and games older than nothing here don't count for this role
    assert tiers([Game(1, "mid", True, NOW - DAY)] * 20) == {}
    # mastery: needs 50,000 points, played within 180 days, and a 10% role share; 200,000+ is comfortable
    m = tiers([], [Mastery(1, 49_999, NOW), Mastery(2, 50_000, NOW), Mastery(3, 200_000, NOW), Mastery(4, 900_000, NOW)])
    assert m == {3: ("comfortable", "mastery"), 2: ("learning", "mastery")}  # 1: too few points; 4: not played in this role
    # games already give five champions: mastery adds nothing
    five = games(1, 5) + games(2, 5) + games(3, 5) + games(4, 5) + games(5, 5)
    assert all(src == "games" for _, src in tiers(five, [Mastery(9, 900_000, NOW)]).values())
    assert len(proposal([g for c in range(1, 15) for g in games(c, 5)], [], "top", SHARES, NOW)) == 10  # at most ten


class FakeRiot:
    """Two ranked games on champions 1 and 2, mastery on 3, Gold. `Name#EUW` is spelled `Canonical#EUW` by the account."""
    requests = 0

    def account(self, riot_id):
        return {"puuid": "api-puuid", "gameName": "Canonical", "tagLine": "EUW"} if riot_id.lower() == "name#euw" else None

    def summoner(self, puuid):
        return {"summonerLevel": 42}

    def ranks(self, puuid):
        return [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 40}]

    def match_ids(self, *args, **kwargs):
        return ["EUW1_1", "EUW1_2", "EUW1_3"]

    def match(self, match_id):
        FakeRiot.requests += 1
        return {"info": {"gameDuration": 120 if match_id.endswith("3") else 1200, "queueId": 440, "gameStartTimestamp": NOW_MS - DAY,
                         "participants": [{"puuid": "api-puuid", "championId": 1 if match_id.endswith("1") else 2, "teamPosition": "TOP",
                                           "win": True, "kills": 5, "deaths": 2, "assists": 7, "totalMinionsKilled": 180,
                                           "neutralMinionsKilled": 4, "visionScore": 18}]}}

    def mastery_all(self, puuid):
        return [{"championId": 3, "championPoints": 60_000, "lastPlayTime": NOW_MS}]

    def close(self):
        pass


@pytest.fixture
def db(engine, session):
    for champ_id in (1, 2, 3):
        session.add(Champion(champ_id=champ_id, key=f"C{champ_id}", name=f"Champ {champ_id}", patch="16.21.1"))
    session.commit()
    return engine


def wait(client, job):
    for _ in range(200):
        status = client.get(f"/api/players/onboard/{job}").json()
        if status["status"] != "reading":
            return status
        time.sleep(0.01)
    raise AssertionError("onboarding never finished")


@pytest.fixture
def svc(make_services, db, monkeypatch, tmp_path):
    svc = make_services(db=db)
    svc.roster_path = tmp_path / "roster.json"
    monkeypatch.setattr(svc, "make_riot", lambda: lambda: FakeRiot())
    monkeypatch.setattr(svc, "learn", lambda: False)  # the learning job would call the real API
    svc.live.engine._role_shares = {1: {"top": 1.0}, 2: {"top": 1.0}, 3: {"top": 1.0}}
    return svc


def test_the_whole_add_player_flow_against_the_fake_client(svc, db):
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        found = client.post("/api/players/lookup", json={"riot_id": " name#euw "}).json()
        assert found["found"] and found["riot_id"] == "Canonical#EUW" and found["level"] == 42  # the account's own spelling
        assert found["rank"]["best"]["label"] == "Gold II" and found["rank"]["best"]["lp"] == 40
        assert client.post("/api/players/lookup", json={"riot_id": "Nobody#EUW"}).json() == {"found": False}
        assert client.post("/api/players/onboard", json={"riot_id": "Name#EUW", "role": "carry"}).status_code == 422

        job = client.post("/api/players/onboard", json={"riot_id": "Name#EUW", "role": "top"}).json()["job"]
        status = wait(client, job)
        assert status["status"] == "done" and (status["games_read"], status["games_total"]) == (3, 3)
        assert status["riot_id"] == "Canonical#EUW" and status["rank"]["best"]["label"] == "Gold II"
        # the remake isn't a game; one game each in the last 14 days is "learning"; mastery fills in behind the games
        assert [(r["champ_id"], r["tier"], r["source"], r["champ"]["id"]) for r in status["proposal"]] == [
            (1, "learning", "games", 1), (2, "learning", "games", 2), (3, "learning", "mastery", 3)]
        assert client.get("/api/players/onboard/nope").status_code == 404

        pool = [{"champ_id": 1, "role": "top", "tier": "main"}, {"champ_id": 3, "role": "top", "tier": "learning"}]  # reviewed
        assert client.post("/api/players", json={"riot_id": "Canonical#EUW", "role": "top", "active": True,
                                                 "pool": pool + [{"champ_id": 99, "tier": "main"}]}).status_code == 422
        roster = client.post("/api/players", json={"riot_id": "Canonical#EUW", "role": "top", "active": True, "pool": pool}).json()
        top = roster["lineup"][0]
        assert top["riot_id"] == "Canonical#EUW" and top["pool"]["size"] == 2 and top["rank"]["label"] == "Gold II"
        assert top["games"] == 2 and top["kda"] == pytest.approx(6.0) and top["stat_games"] == 2  # figures at once
        assert "Canonical#EUW" in svc.roster_path.read_text(encoding="utf-8")
        again = client.post("/api/players", json={"riot_id": "canonical#euw", "role": "mid", "active": False, "pool": []})
        assert again.status_code == 422 and "already on the roster" in again.json()["detail"]
        known = client.post("/api/players/lookup", json={"riot_id": "canonical#euw"}).json()
        assert known["on_roster"] and known["role"] == "top" and known["active"]

    with Session(db) as s:
        player = s.exec(select(Player).where(Player.riot_id == "Canonical#EUW")).one()
        assert player.api_puuid == "api-puuid" and player.mastery_json and player.rank_json and player.profile_fetched_at
        assert {(e.champ_id, e.manual_tier) for e in s.exec(select(PoolEntry).where(PoolEntry.puuid == player.puuid))} == {
            (1, "main"), (3, "learning")}
        stored = s.exec(select(PlayerGame).where(PlayerGame.puuid == player.puuid)).all()
        # under their real match ids, so the learning job's fetch recognises them instead of adding them again
        assert {g.match_id for g in stored} == {"EUW1_1", "EUW1_2"} and all(g.source == "match-v5" and g.kills == 5 for g in stored)


def test_adding_to_the_bench_leaves_the_lineup_alone_and_active_benches_whoever_was_there(svc, db):
    set_players(db, [{"role": "top", "riot_id": "Old#EUW"}])
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        benched = client.post("/api/players", json={"riot_id": "Sub#EUW", "role": "top", "active": False, "pool": []}).json()
        assert benched["lineup"][0]["riot_id"] == "Old#EUW" and [b["riot_id"] for b in benched["bench"]] == ["Sub#EUW"]
        active = client.post("/api/players", json={"riot_id": "New#EUW", "role": "top", "active": True,
                                                   "pool": [{"champ_id": 2, "tier": "comfortable"}]}).json()
        assert active["lineup"][0]["riot_id"] == "New#EUW"
        assert sorted(b["riot_id"] for b in active["bench"]) == ["Old#EUW", "Sub#EUW"]
        dup = client.post("/api/players", json={"riot_id": "X#EUW", "role": "top", "active": False,
                                                "pool": [{"champ_id": 2, "tier": "main"}, {"champ_id": 2, "tier": "learning"}]})
        assert dup.status_code == 422


def test_skip_goes_on_with_what_has_been_read(svc, db, monkeypatch):
    release = threading.Event()

    class Slow(FakeRiot):
        def match(self, match_id):
            if not match_id.endswith("1"):
                release.wait(5)
            return super().match(match_id)

    monkeypatch.setattr(svc, "make_riot", lambda: lambda: Slow())
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        job = client.post("/api/players/onboard", json={"riot_id": "Name#EUW", "role": "top"}).json()["job"]
        for _ in range(200):
            if client.get(f"/api/players/onboard/{job}").json()["games_read"] >= 1:
                break
            time.sleep(0.01)
        assert client.post(f"/api/players/onboard/{job}/skip").status_code == 200
        release.set()
        status = wait(client, job)
        assert status["status"] == "done" and status["games_read"] < status["games_total"]  # it stopped early
        assert [r["champ_id"] for r in status["proposal"]][0] == 1


def test_without_a_key_the_pool_is_built_by_hand(make_services, db):
    with TestClient(create_app(make_services(db=db), stats_refresh_s=0)) as client:
        assert client.post("/api/players/lookup", json={"riot_id": "Name#EUW"}).json() == {"no_key": True}
        job = client.post("/api/players/onboard", json={"riot_id": "Name#EUW", "role": "top"}).json()["job"]
        status = client.get(f"/api/players/onboard/{job}").json()
        assert status["status"] == "done" and status["proposal"] == [] and status["games_total"] == 0 and status["error"] is None
        roster = client.post("/api/players", json={"riot_id": "Name#EUW", "role": "top", "active": True,
                                                   "pool": [{"champ_id": 1, "tier": "main"}]}).json()
        assert roster["lineup"][0]["riot_id"] == "Name#EUW" and roster["lineup"][0]["games"] == 0 and not roster["has_key"]


def test_a_failed_read_says_why(svc, monkeypatch):
    class Broken(FakeRiot):
        def match_ids(self, *args, **kwargs):
            raise ConnectionError("down")

    monkeypatch.setattr(svc, "make_riot", lambda: lambda: Broken())
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        gone = wait(client, client.post("/api/players/onboard", json={"riot_id": "Nobody#EUW", "role": "top"}).json()["job"])
        assert gone["status"] == "error" and gone["error"] == "No such account"
        broken = wait(client, client.post("/api/players/onboard", json={"riot_id": "Name#EUW", "role": "top"}).json()["job"])
        assert broken["status"] == "error" and "ConnectionError" in broken["error"] and broken["rank"]["best"]["label"] == "Gold II"
