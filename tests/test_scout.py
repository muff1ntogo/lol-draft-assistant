"""docs/enemy-scouting.md, Stage A: scouting data."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.riot import RiotAuthError
from draftassist.data.scout import (
    MAX_GAMES,
    REFRESH_S,
    ScoutRunner,
    identity_report,
    load_pools,
    mastery_age,
    parse_riot_ids,
    scout,
)
from draftassist.db.models import ScoutGame, ScoutPlayer

NOW = 1_800_000_000.0
DAY_MS = 86_400_000


class FakeRiot:
    """Five players; each has `games` ranked matches on champions 100 + i (top), with mastery on 200 + i."""

    def __init__(self, games=3, missing=(), auth_error=False, fail_match=None):
        self.calls: list[tuple] = []
        self.games, self.missing, self.auth_error, self.fail_match = games, set(missing), auth_error, fail_match

    def account_puuid(self, riot_id):
        self.calls.append(("account", riot_id))
        if self.auth_error:
            raise RiotAuthError("bad key")
        return None if riot_id in self.missing else f"puuid-{riot_id.split('#')[0].lower()}"

    def mastery_top(self, puuid, count=20):
        self.calls.append(("mastery", puuid))
        return [{"championId": 200 + n, "championPoints": 1000 * (5 - n), "lastPlayTime": int(NOW * 1000) - n * DAY_MS}
                for n in range(3)]

    def match_ids(self, puuid, queue, start_time, count=20, start=0, end_time=None):
        self.calls.append(("ids", puuid))
        return [f"EUW1_{puuid}_{n}" for n in range(self.games)][:count]

    def match(self, match_id):
        self.calls.append(("match", match_id))
        if match_id == self.fail_match:
            raise RuntimeError("boom")
        _, puuid, n = match_id.split("_")
        n = int(n)
        return {"info": {"gameDuration": 1800, "queueId": 440, "gameStartTimestamp": int(NOW * 1000) - (n + 1) * DAY_MS,
                         "participants": [{"puuid": puuid, "championId": 100 + n % 2, "teamPosition": "TOP",
                                           "win": n % 2 == 0},
                                          {"puuid": "someone-else", "championId": 999, "teamPosition": "MIDDLE", "win": True}]}}


TEAM = ["Alpha#EUW", "Bravo#EUW", "Charlie#EUW", "Delta#EUW", "Echo#EUW"]


# ---------- pasting Riot IDs ----------
def test_riot_ids_from_lines_commas_and_lobby_text():
    text = "Alpha#EUW\nBravo #1234 joined the lobby\nCharlie Two#EUW, Delta#xyz\nnot an id\nalpha#euw"
    assert parse_riot_ids(text) == ["Alpha#EUW", "Bravo#1234", "Charlie Two#EUW", "Delta#xyz"]


def test_at_most_a_team():
    assert len(parse_riot_ids("\n".join(f"P{i}x#EUW" for i in range(8)))) == 5


# ---------- fetching ----------
def test_mastery_for_everyone_comes_before_any_match(engine):
    riot = FakeRiot()
    scout(engine, riot, TEAM, now=NOW)
    kinds = [c[0] for c in riot.calls]
    last_mastery, first_match = max(i for i, k in enumerate(kinds) if k == "mastery"), kinds.index("match")
    assert last_mastery < first_match


def test_matches_are_read_one_player_at_a_time(engine):
    riot = FakeRiot(games=3)
    scout(engine, riot, TEAM[:2], now=NOW)
    order = [c[1].split("_")[1] for c in riot.calls if c[0] == "match"]
    assert order == ["puuid-alpha", "puuid-bravo"] * 3


def test_a_pool_has_games_per_role_and_mastery(engine):
    statuses = scout(engine, FakeRiot(games=4), TEAM[:1], now=NOW)
    assert statuses == {"Alpha#EUW": "done"}
    pool = load_pools(engine, ["alpha#euw"], now=NOW)["alpha#euw"]  # Riot IDs match whatever the case
    assert pool.games == {"top": {100: (2, 2), 101: (2, 0)}}
    assert pool.mastery[0] == (200, 5000) and pool.games_read == 4 == pool.games_wanted
    assert pool.mastery_last == {200 + n: int(NOW * 1000) - n * DAY_MS for n in range(3)}  # Stage E: recency
    assert dict(mastery_age(pool, now=NOW)) == {200: 0.0, 201: 1.0, 202: 2.0}


def test_mastery_stored_before_stage_e_still_loads(engine):
    with Session(engine) as s:
        s.add(ScoutPlayer(riot_id="Old#EUW", api_puuid="p-old", status="done", mastery_json="[[1, 500]]"))
        s.commit()
    pool = load_pools(engine, ["Old#EUW"])["Old#EUW"]
    assert pool.mastery == ((1, 500),) and pool.mastery_last == {}


def test_a_game_without_a_team_position_uses_the_individual_one(engine):
    class NoTeamPosition(FakeRiot):
        def match(self, match_id):
            body = super().match(match_id)
            body["info"]["participants"][0].update(teamPosition="", individualPosition="JUNGLE")
            return body

    scout(engine, NoTeamPosition(games=2), TEAM[:1], now=NOW)
    assert set(load_pools(engine, TEAM[:1], now=NOW)["Alpha#EUW"].games) == {"jungle"}


def test_a_missing_account_is_marked_and_the_rest_go_on(engine):
    statuses = scout(engine, FakeRiot(missing={"Bravo#EUW"}), TEAM[:2], now=NOW)
    assert statuses == {"Alpha#EUW": "done", "Bravo#EUW": "missing"}


def test_one_bad_match_doesnt_stop_the_player(engine):
    riot = FakeRiot(games=3, fail_match="EUW1_puuid-alpha_1")
    assert scout(engine, riot, TEAM[:1], now=NOW) == {"Alpha#EUW": "done"}
    assert load_pools(engine, TEAM[:1], now=NOW)["Alpha#EUW"].games_read == 2


def test_a_bad_key_stops_everything(engine):
    with pytest.raises(RiotAuthError):
        scout(engine, FakeRiot(auth_error=True), TEAM, now=NOW)


def test_a_recent_scout_isnt_fetched_again_unless_forced(engine):
    scout(engine, FakeRiot(), TEAM[:1], now=NOW)
    again = FakeRiot()
    assert scout(engine, again, TEAM[:1], now=NOW + 60) == {"Alpha#EUW": "done"} and again.calls == []
    assert scout(engine, again, TEAM[:1], now=NOW + 60, force=True) == {"Alpha#EUW": "done"} and again.calls
    later = FakeRiot()
    scout(engine, later, TEAM[:1], now=NOW + 60 + REFRESH_S + 1)  # after the forced fetch at NOW + 60
    assert later.calls


def test_matches_already_stored_arent_fetched_again(engine):
    scout(engine, FakeRiot(games=2), TEAM[:1], now=NOW)
    riot = FakeRiot(games=2)
    scout(engine, riot, TEAM[:1], now=NOW, force=True)
    assert not [c for c in riot.calls if c[0] == "match"]


def test_old_games_fall_out_of_the_pool(engine):
    scout(engine, FakeRiot(games=3), TEAM[:1], now=NOW)
    assert load_pools(engine, TEAM[:1], now=NOW + 59 * 86_400)["Alpha#EUW"].games_read == 1


def test_at_most_max_games_are_read(engine):
    riot = FakeRiot(games=MAX_GAMES + 10)
    scout(engine, riot, TEAM[:1], now=NOW)
    assert len([c for c in riot.calls if c[0] == "match"]) == MAX_GAMES


# ---------- in the background ----------
def wait(runner, timeout=5.0):
    deadline = time.monotonic() + timeout
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)


def test_the_runner_fetches_in_the_background_and_tells_us(engine):
    told = []
    runner = ScoutRunner(engine, lambda: FakeRiot(), on_progress=lambda: told.append(1))
    assert runner.enqueue(TEAM[:2]) is True
    wait(runner)
    assert not runner.running and told
    with Session(engine) as s:
        assert {p.status for p in s.exec(select(ScoutPlayer)).all()} == {"done"}


def test_without_a_key_nothing_is_fetched(engine):
    runner = ScoutRunner(engine, None)
    assert runner.available is False and runner.enqueue(TEAM) is False


def test_a_bad_key_is_reported(engine):
    runner = ScoutRunner(engine, lambda: FakeRiot(auth_error=True))
    runner.enqueue(TEAM[:1])
    wait(runner)
    assert runner.last_error == "bad key"


def test_api(make_services, engine):
    svc = make_services(db=engine)
    svc.scout_runner_override = ScoutRunner(engine, lambda: FakeRiot(games=2))
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.post("/api/scout", json={"text": "nothing here"}).status_code == 422
        started = client.post("/api/scout", json={"text": "Alpha#EUW\nBravo#EUW"}).json()
        assert started["available"] is True and [p["riot_id"] for p in started["players"]] == ["Alpha#EUW", "Bravo#EUW"]
        wait(svc.scout_runner_override)
        body = client.get("/api/scout", params=[("ids", "Alpha#EUW"), ("ids", "Bravo#EUW")]).json()
    alpha = body["players"][0]
    assert alpha["status"] == "done" and alpha["games_read"] == 2 and alpha["role"] == "top"  # the same summary as Live's
    assert [t["games"] for t in alpha["top"]] == [1, 1]  # their games in that role, once read
    with Session(engine) as s:
        assert len(s.exec(select(ScoutGame)).all()) == 4


# ---------- what the client gives ----------
def test_identity_report_counts_fields_without_values(tmp_path):
    def session(queue, enemy):
        return {"queueId": queue, "myTeam": [], "theirTeam": [enemy, {"cellId": 9, "gameName": ""}]}

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "0001-session.json").write_text(json.dumps(session(440, {"cellId": 5, "championId": 0})))
    (tmp_path / "a" / "0002-session.json").write_text(json.dumps(
        session(999, {"cellId": 5, "gameName": "Secret Name", "tagLine": "EUW", "assignedPosition": "top"})))
    report = identity_report(tmp_path)
    assert report[440] == {"sessions": 1, "cellId": 1}
    assert report[999] == {"sessions": 1, "assignedPosition": 1, "cellId": 1, "gameName": 1, "tagLine": 1}
    assert "Secret Name" not in json.dumps(report)


def test_scout_check_command(tmp_path, capsys):
    from draftassist import launcher

    (tmp_path / "0001-session.json").write_text(json.dumps(
        {"queueId": 999, "theirTeam": [{"cellId": 5, "gameName": "Secret Name", "tagLine": "EUW"}]}))
    assert launcher.main(["scout-check", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "gameName" in out and "set in 1 sessions" in out and "Secret Name" not in out
    assert launcher.main(["scout-check", str(tmp_path / "empty")]) == 1
