"""docs/player-roster.md: the bench, swapping in subs, and the roster file."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.pools import (
    bench,
    bench_player,
    delete_player,
    forget_player,
    import_pools,
    list_players,
    load_pools,
    load_team,
    parse_pool_text,
    remove_player_pool_entry,
    roles_by_puuid,
    set_player_pool_entry,
    set_players,
    swap_player,
)
from draftassist.data.provider import stats_version
from draftassist.data.roster import restore, save, snapshot
from draftassist.db.models import Champion, Player, PlayerGame, PoolEntry
from draftassist.db.session import init_db

CHAMPS = {897: ("KSante", "K'Sante"), 68: ("Rumble", "Rumble"), 150: ("Gnar", "Gnar"), 113: ("Sejuani", "Sejuani"),
          61: ("Orianna", "Orianna")}
POOLS = "TOP\nK'Sante: main\nRumble: comfortable\n\nJUNGLE\nSejuani: main\n"


@pytest.fixture
def db(engine, session):
    for cid, (key, name) in CHAMPS.items():
        session.add(Champion(champ_id=cid, key=key, name=name, patch="16.21.1"))
    session.commit()
    import_pools(engine, parse_pool_text(POOLS, CHAMPS))
    set_players(engine, [{"role": "top", "riot_id": "Main#EUW"}, {"role": "jungle", "riot_id": "Jg#EUW"}],
                {"Main#EUW": "puuid-main", "Jg#EUW": "puuid-jg"}.get)
    return engine


def top_pool(db):
    return sorted(e.champ_id for e in load_pools(db)["top"])


def by_role(db):
    return {p["role"]: p["riot_id"] for p in list_players(db)}


# ---------- swapping ----------
def test_a_sub_comes_in_with_an_empty_pool_and_the_main_waits_on_the_bench(db):
    swap_player(db, "top", "Sub#EUW", {"Sub#EUW": "puuid-sub"}.get)
    assert by_role(db)["top"] == "Sub#EUW" and top_pool(db) == []
    benched = bench(db)
    assert [(b["riot_id"], b["role"]) for b in benched] == [("Main#EUW", "top")]
    assert sorted(c for c, _ in benched[0]["pools"]["top"]) == [68, 897]
    assert "top" not in load_team(db).pools or not load_team(db).pools["top"]
    assert roles_by_puuid(db).get("puuid-main") is None and roles_by_puuid(db)["puuid-sub"] == "top"


def test_swapping_back_brings_the_pool_back(db):
    swap_player(db, "top", "Sub#EUW")
    import_pools(db, parse_pool_text("TOP\nGnar: main", CHAMPS))  # the sub's own pool
    swap_player(db, "top", "main#euw")  # Riot IDs match whatever the case
    assert by_role(db)["top"] == "Main#EUW" and top_pool(db) == [68, 897]
    assert [b["riot_id"] for b in bench(db)] == ["Sub#EUW"] and bench(db)[0]["pools"] == {"top": [(150, "main")]}


def test_a_player_active_in_another_role_isnt_taken(db):
    with pytest.raises(ValueError, match="our jungle"):
        swap_player(db, "top", "Jg#EUW")
    with pytest.raises(ValueError, match="Riot ID"):
        swap_player(db, "top", "")


def test_a_benched_player_can_play_another_role(db):
    swap_player(db, "top", "Sub#EUW")
    swap_player(db, "mid", "Main#EUW")  # their top pool waits under top
    assert by_role(db)["mid"] == "Main#EUW" and load_pools(db)["mid"] == []
    swap_player(db, "mid", "Other#EUW")
    swap_player(db, "top", "Main#EUW")
    assert top_pool(db) == [68, 897]


def test_saving_a_new_riot_id_benches_a_linked_player_but_renames_one_never_linked(db):
    set_players(db, [{"role": "top", "riot_id": "Sub#EUW"}])
    assert top_pool(db) == [] and [b["riot_id"] for b in bench(db)] == ["Main#EUW"]
    set_players(db, [{"role": "top", "riot_id": "Sbu#EUW"}])  # the sub isn't linked: a typo fix, not a swap
    assert by_role(db)["top"] == "Sbu#EUW" and [b["riot_id"] for b in bench(db)] == ["Main#EUW"]
    set_players(db, [{"role": "top", "riot_id": "Main#EUW"}])  # back from the bench
    assert top_pool(db) == [68, 897]


def test_two_unlinked_players_in_a_role_get_their_own_placeholders(engine):
    set_players(engine, [{"role": "mid", "riot_id": "A#EUW"}])
    swap_player(engine, "mid", "B#EUW")
    with Session(engine) as s:
        keys = sorted(p.puuid for p in s.exec(select(Player)).all())
    assert keys == ["unresolved:mid", "unresolved:mid:2"]


def test_forgetting_only_takes_benched_players(db):
    with Session(db) as s:
        s.add(PlayerGame(puuid="puuid-main", match_id="EUW1_1", champ_id=897, role="top", win=True, game_start_ms=1,
                         source="match-v5"))
        s.commit()
    assert not forget_player(db, "Main#EUW")  # still playing
    swap_player(db, "top", "Sub#EUW")
    assert forget_player(db, "Main#EUW") and bench(db) == []
    with Session(db) as s:
        assert s.exec(select(PoolEntry).where(PoolEntry.puuid == "puuid-main")).first() is None
        assert s.exec(select(PlayerGame).where(PlayerGame.puuid == "puuid-main")).first() is None


def test_a_swap_changes_the_stats_version_so_the_engine_reloads_after_the_draft(db):
    before = stats_version(db)
    swap_player(db, "top", "Sub#EUW")
    assert stats_version(db) != before


def test_bench_activate_and_delete_keep_the_right_player_data(db):
    """Stage C: bench keeps the pool, activating brings the role's pool back, delete removes the pool and the fetched
    games and keeps the team's logged games (decision 6)."""
    with Session(db) as s:
        before = s.get(Player, "puuid-main").active_since
        s.add(PlayerGame(puuid="puuid-main", match_id="fetched", champ_id=897, role="top", win=True, game_start_ms=1,
                         source="match-v5"))
        s.add(PlayerGame(puuid="puuid-main", match_id="logged", champ_id=897, role="top", win=True, game_start_ms=2,
                         source="logged"))
        s.commit()
    assert bench_player(db, "Main#EUW") and not bench_player(db, "Main#EUW") and not bench_player(db, "Nobody#EUW")
    assert [b["riot_id"] for b in bench(db)] == ["Main#EUW"] and top_pool(db) == []
    assert sorted(c for c, _ in bench(db)[0]["pools"]["top"]) == [68, 897]  # the bench keeps the pool
    swap_player(db, "top", "main#euw")
    assert top_pool(db) == [68, 897]  # and it comes back with them
    with Session(db) as s:
        main = s.get(Player, "puuid-main")
        assert main.active and main.active_since >= before
    assert delete_player(db, "Main#EUW") and not delete_player(db, "Main#EUW")
    with Session(db) as s:
        assert s.get(Player, "puuid-main") is None
        assert s.exec(select(PoolEntry).where(PoolEntry.puuid == "puuid-main")).first() is None
        assert s.get(PlayerGame, ("puuid-main", "fetched")) is None
        assert s.get(PlayerGame, ("puuid-main", "logged")) is not None  # team history stays
    assert by_role(db)["top"] == ""  # the role is empty, not a nameless player


def test_any_players_pool_can_be_edited_benched_or_not(db):
    swap_player(db, "top", "Sub#EUW", {"Sub#EUW": "puuid-sub"}.get)
    assert set_player_pool_entry(db, "Main#EUW", "top", 150, "learning")  # on the bench
    assert set_player_pool_entry(db, "sub#euw", "top", 150, "main") and not set_player_pool_entry(db, "Nobody#EUW", "top", 150, "main")
    assert top_pool(db) == [150]
    assert sorted(c for c, _ in bench(db)[0]["pools"]["top"]) == [68, 150, 897]
    assert remove_player_pool_entry(db, "Main#EUW", "top", 150) and not remove_player_pool_entry(db, "Main#EUW", "top", 150)
    with pytest.raises(ValueError):
        set_player_pool_entry(db, "Main#EUW", "top", 150, "godlike")


# ---------- the roster file ----------
def test_the_file_round_trips_into_an_empty_database(db, tmp_path):
    with Session(db) as s:
        e = s.get(PoolEntry, ("puuid-main", 897, "top"))
        e.tier_locked = True
        s.add(e)
        s.commit()
    swap_player(db, "top", "Sub#EUW")
    path = tmp_path / "data" / "roster.json"
    assert save(db, path, lambda c: CHAMPS[c][1])
    body = json.loads(path.read_text(encoding="utf-8"))
    assert "puuid" not in path.read_text(encoding="utf-8")  # no identities beyond the Riot IDs
    assert {p["riot_id"]: p["active"] for p in body["players"]} == {"Sub#EUW": True, "Jg#EUW": True, "Main#EUW": False}

    fresh = init_db(tmp_path / "fresh.sqlite")
    assert restore(fresh, path) == 3
    assert by_role(fresh) == {"top": "Sub#EUW", "jungle": "Jg#EUW", "mid": "", "adc": "", "support": ""}
    assert [b["riot_id"] for b in bench(fresh)] == ["Main#EUW"]
    with Session(fresh) as s:
        locked = [e for e in s.exec(select(PoolEntry)).all() if e.tier_locked]
    assert [(e.champ_id, e.manual_tier) for e in locked] == [(897, "main")]
    assert snapshot(fresh)["players"] == snapshot(db)["players"]


def test_a_database_with_a_roster_is_never_overwritten_by_the_file(db, tmp_path):
    path = tmp_path / "roster.json"
    save(db, path)
    swap_player(db, "top", "Sub#EUW")
    assert restore(db, path) == 0 and by_role(db)["top"] == "Sub#EUW"


def test_an_empty_database_never_overwrites_a_good_file(db, tmp_path):
    path = tmp_path / "roster.json"
    save(db, path)
    good = path.read_text(encoding="utf-8")
    assert not save(init_db(tmp_path / "empty.sqlite"), path)
    assert path.read_text(encoding="utf-8") == good


def test_a_broken_file_falls_back_to_the_previous_one(db, tmp_path):
    path = tmp_path / "roster.json"
    save(db, path)
    swap_player(db, "top", "Sub#EUW")
    save(db, path)  # the first one is now roster.json.bak
    path.write_text("{not json", encoding="utf-8")
    assert restore(init_db(tmp_path / "fresh.sqlite"), path) == 2  # the .bak: before the sub came in


# ---------- the Pools page ----------
def test_api_swap_bench_and_the_file_follows(make_services, db, tmp_path):
    svc = make_services(db=db)
    svc.roster_path = tmp_path / "roster.json"
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        body = client.post("/api/players/swap", json={"role": "top", "riot_id": "Sub#EUW"}).json()
        assert next(p for p in body["players"] if p["role"] == "top")["riot_id"] == "Sub#EUW"
        assert body["bench"][0]["riot_id"] == "Main#EUW" and body["bench"][0]["pools"]["top"][0]["tier"] == "main"
        assert body["applied"] is True
        saved = json.loads(svc.roster_path.read_text(encoding="utf-8"))
        assert {p["riot_id"] for p in saved["players"]} == {"Sub#EUW", "Jg#EUW", "Main#EUW"}
        assert client.post("/api/players/swap", json={"role": "top", "riot_id": "Jg#EUW"}).status_code == 422
        assert client.delete("/api/players/bench", params={"riot_id": "Nobody#EUW"}).status_code == 404
        assert client.delete("/api/players/bench", params={"riot_id": "Main#EUW"}).json()["bench"] == []
        assert "Main#EUW" not in svc.roster_path.read_text(encoding="utf-8")


def test_api_roster_profile_and_actions(make_services, db, tmp_path):
    svc = make_services(db=db)
    svc.roster_path = tmp_path / "roster.json"
    file = lambda: svc.roster_path.read_text(encoding="utf-8")
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        roster = client.get("/api/roster").json()
        assert [r["role"] for r in roster["lineup"]] == ["top", "jungle", "mid", "adc", "support"]
        top, mid = roster["lineup"][0], roster["lineup"][2]
        assert top["riot_id"] == "Main#EUW" and top["since_days"] == 0 and top["pool"]["size"] == 2 and top["pool_roles"] == ["top"]
        assert {c["champ"]["name"] for c in top["pool"]["champs"]} == {"Rumble", "K'Sante"}
        assert mid == {"role": "mid", "role_label": "Mid", "riot_id": "", "active": False}  # an empty role
        assert roster["bench"] == [] and roster["has_key"] is False

        profile = client.get("/api/players/Main%23EUW/profile", params={"window": "60d"}).json()
        assert profile["riot_id"] == "Main#EUW" and profile["active"] and profile["pools"][0]["editable_tiers"]
        assert [r["champ"]["name"] for r in profile["pools"][0]["pool"]] == ["K'Sante", "Rumble"]
        assert client.get("/api/players/Main%23EUW/profile", params={"window": "bad"}).status_code == 422
        assert client.get("/api/players/Nobody%23EUW/profile").status_code == 404

        # the pool of a named player, and the stats bracket
        assert client.put("/api/players/Main%23EUW/pool/top/150", json={"tier": "learning"}).json() == {"applied": True}
        assert client.put("/api/players/Main%23EUW/pool/top/150", json={"tier": "godlike"}).status_code == 422
        assert client.put("/api/players/Main%23EUW/pool/top/4242", json={"tier": "main"}).status_code == 404
        assert client.delete("/api/players/Main%23EUW/pool/top/150").json() == {"applied": True}
        assert client.delete("/api/players/Main%23EUW/pool/top/150").status_code == 404
        assert client.put("/api/players/Main%23EUW/bracket", json={"bracket": "Emerald"}).json() == {"bracket": "emerald"}
        assert client.get("/api/roster").json()["lineup"][0]["bracket"] == "emerald"

        # bench, activate with what to undo, delete with confirm; the roster file follows each
        benched = client.post("/api/players/Main%23EUW/bench").json()
        assert benched["lineup"][0]["riot_id"] == "" and [b["riot_id"] for b in benched["bench"]] == ["Main#EUW"]
        assert '"active": false' in file() and client.post("/api/players/Main%23EUW/bench").status_code == 404
        assert client.post("/api/players/Jg%23EUW/activate", json={"role": "jungle"}).json()["undo"] is None  # already there
        back = client.post("/api/players/Main%23EUW/activate", json={"role": "top"}).json()
        assert back["roster"]["lineup"][0]["riot_id"] == "Main#EUW" and back["undo"] == {"riot_id": "", "role": "top", "bench": "Main#EUW"}
        assert client.post("/api/players/Main%23EUW/activate", json={"role": "jungle"}).status_code == 422  # they are our top
        assert client.post("/api/players/Nobody%23EUW/activate", json={"role": "top"}).status_code == 404
        assert client.delete("/api/players/Main%23EUW", params={"confirm": "wrong"}).status_code == 422
        assert client.delete("/api/players/Main%23EUW").status_code == 422
        gone = client.delete("/api/players/Main%23EUW", params={"confirm": "main#euw"}).json()
        assert gone["lineup"][0]["riot_id"] == "" and "Main#EUW" not in file()
        assert client.delete("/api/players/Main%23EUW", params={"confirm": "Main#EUW"}).status_code == 404


def test_a_pasted_pool_is_read_for_one_player_without_saving(make_services, db):
    with TestClient(create_app(make_services(db=db), stats_refresh_s=0)) as client:
        bare = client.post("/api/pools/parse", json={"role": "top", "text": "Gnar: main\nRumble: learning\nNobody: main"}).json()
        assert [(e["champ_id"], e["tier"], e["champ"]["name"]) for e in bare["entries"]] == [(150, "main", "Gnar"), (68, "learning", "Rumble")]
        assert len(bare["errors"]) == 1 and bare["errors"][0].startswith("line 3: unknown champion")  # the user's own line number
        headed = client.post("/api/pools/parse", json={"role": "top", "text": "Top:\nGnar: main\n\nJUNGLE\nSejuani: main"}).json()
        assert [e["champ_id"] for e in headed["entries"]] == [150] and "jungle ignored" in headed["errors"][0]
        assert client.post("/api/pools/parse", json={"role": "carry", "text": "Gnar: main"}).status_code == 422
    assert top_pool(db) == [68, 897]  # nothing was saved
