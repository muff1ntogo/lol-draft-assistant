"""Pool editor on the Pools page: add, retier or remove one champion per player, and every pool change
(editor or import) picks up the player's stored games straight away instead of at the next learning run."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.pools import import_pools, parse_pool_text, remove_pool_entry, set_pool_entry
from draftassist.db.models import Player, PoolEntry
from draftassist.learning.comfort import comfort_score
from test_learning import add_games, db, entry  # noqa: F401  (db is a fixture)

AHRI, JINX, LEONA = 103, 222, 89
NAMES = {AHRI: ("Ahri", "Ahri"), JINX: ("Jinx", "Jinx"), LEONA: ("Leona", "Leona")}


def mid_puuid(db):
    with Session(db) as s:
        return s.exec(select(Player).where(Player.role == "mid")).one().puuid


def test_import_counts_the_players_stored_games(db):
    import_pools(db, parse_pool_text("MID\nJinx: learning", NAMES))  # creates the mid player
    add_games(db, mid_puuid(db), AHRI, "mid", 3, 1)  # games already fetched for a champion not in the pool
    import_pools(db, parse_pool_text("MID\nAhri: comfortable", NAMES))
    ahri = entry(db, AHRI, "mid")
    assert (ahri.games, ahri.wins) == (4, 3)
    assert ahri.comfort == pytest.approx(comfort_score("comfortable", 4, 3))


def test_add_retier_and_remove_one_champion(db):
    import_pools(db, parse_pool_text("MID\nJinx: learning", NAMES))
    add_games(db, mid_puuid(db), AHRI, "mid", 2, 0)
    set_pool_entry(db, "mid", AHRI, "main")
    ahri = entry(db, AHRI, "mid")
    assert (ahri.manual_tier, ahri.games, ahri.wins) == ("main", 2, 2)
    set_pool_entry(db, "mid", AHRI, "learning")  # retier keeps lock and history
    assert entry(db, AHRI, "mid").manual_tier == "learning"
    assert remove_pool_entry(db, "mid", AHRI) is True
    assert remove_pool_entry(db, "mid", AHRI) is False
    with Session(db) as s:
        assert [e.champ_id for e in s.exec(select(PoolEntry).where(PoolEntry.role == "mid")).all()] == [JINX]
    with pytest.raises(ValueError):
        set_pool_entry(db, "mid", AHRI, "godlike")
    with pytest.raises(ValueError):
        set_pool_entry(db, "middle", AHRI, "main")


def test_editor_api_updates_the_engine(make_services, db):
    svc = make_services(db=db)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        body = client.get("/api/pools").json()
        assert {"id": AHRI, "name": "Ahri"} in body["champions"]  # for the add box
        r = client.put(f"/api/pools/support/{LEONA}", json={"tier": "main"})
        assert r.status_code == 200 and r.json()["applied"] is True
        assert [e["champ"]["id"] for e in r.json()["pools"]["support"]] == [LEONA]
        assert svc.engine.team.entry(LEONA, "support") is not None
        assert client.put(f"/api/pools/support/{LEONA}", json={"tier": "nope"}).status_code == 422
        assert client.put("/api/pools/support/999999", json={"tier": "main"}).status_code == 404
        r = client.delete(f"/api/pools/support/{LEONA}")
        assert r.status_code == 200 and r.json()["pools"]["support"] == []
        assert client.delete(f"/api/pools/support/{LEONA}").status_code == 404
