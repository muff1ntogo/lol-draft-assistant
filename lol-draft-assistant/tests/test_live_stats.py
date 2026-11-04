"""Crawl data reaches a running app: the crawl aggregates as it goes, the app reloads between drafts."""
import sqlite3
import time

from sqlalchemy import text
from sqlmodel import Session
from test_data_pipeline import (
    BLUE,
    RED,
    FakeRiot,
    add_champions,
    make_match,
    riot_client,
)

from draftassist.data.aggregate import aggregate
from draftassist.data.crawler import Crawler
from draftassist.data.opgg import import_bootstrap
from draftassist.data.provider import load_engine_inputs
from draftassist.db.models import BracketTotal, SeedPlayer
from draftassist.db.session import init_db


def bootstrap_rows():
    return [{"champion": f"Champ{c}", "role": ("top", "jungle", "mid", "adc", "support")[i % 5], "play": 1000,
             "win": 510, "pick_rate": 0.1, "ban_rate": 0.0, "role_rate": 0.9} for i, c in enumerate(BLUE + RED)]


def test_crawler_calls_checkpoint_while_running(engine, session, tmp_path):
    add_champions(session, BLUE + RED)
    with Session(engine) as s:
        for p in ("p1", "p2", "p3"):
            s.add(SeedPlayer(puuid=p, bracket="gold", tier="GOLD", division="I"))
        s.commit()
    fake = FakeRiot({p: [make_match(f"EUW1_5{i}", BLUE, RED)] for i, p in enumerate(("p1", "p2", "p3"))})
    clock = iter(range(1_793_000_000, 1_793_100_000, 100))  # every clock read moves 100 s forward
    crawler = Crawler(riot_client(tmp_path, fake), engine, "16.21", brackets=("gold",), clock=lambda: next(clock))
    seen = []
    crawler.run(max_matches=10, checkpoint=lambda st: seen.append((st.total_stored, aggregate(engine, "16.21"))),
                checkpoint_every_s=150)
    assert seen and seen[0][1] == {"gold": seen[0][0]}  # stats tables already reflect the run mid-crawl


def test_wal_lets_the_app_read_while_the_crawler_writes(tmp_path):
    db = init_db(tmp_path / "wal.sqlite")
    with db.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    writer = sqlite3.connect(tmp_path / "wal.sqlite", timeout=0.1)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO bracket_totals (patch, bracket, matches) VALUES ('16.21', 'gold', 5)")
    t0 = time.perf_counter()
    with db.connect() as conn:  # an uncommitted write must not block readers
        assert conn.execute(text("SELECT COUNT(*) FROM bracket_totals")).scalar() == 0
    assert time.perf_counter() - t0 < 1.0
    writer.commit()
    writer.close()


def test_app_reloads_stats_between_drafts_only(make_services, engine, session):
    add_champions(session, BLUE + RED)
    svc = make_services(db=engine)
    assert svc.refresh_stats() == "reloaded"  # first check builds from the database
    assert svc.data_source == "fixture"
    assert svc.refresh_stats() == "unchanged"

    import_bootstrap(engine, "16.21", bootstrap_rows())  # new stats arrive...
    svc.live.handle_phase("ChampSelect", None)  # ...during a draft
    assert svc.refresh_stats() == "deferred"
    assert svc.data_source == "fixture"  # suggestions don't change mid-draft

    svc.live.handle_phase("InProgress", None)
    assert svc.refresh_stats() == "reloaded"
    assert svc.data_source == "opgg"
    assert svc.hub.latest["status"] == "waiting" and "OP.GG" in svc.hub.latest["note"]

    with Session(engine) as s:  # an aggregation run changes the version again
        s.add(BracketTotal(patch="16.21", bracket="gold", matches=450))
        s.commit()
    svc.live.handle_phase("Lobby", None)
    assert svc.refresh_stats() == "reloaded"
    assert svc.refresh_stats() == "unchanged"


def test_pool_import_does_not_trigger_a_second_reload(make_services, engine, session):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    add_champions(session, BLUE + RED)
    svc = make_services(db=engine)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.post("/api/pools/import", json={"text": "TOP\nK'Sante: main"}).status_code == 200
    assert svc.refresh_stats() == "unchanged"


def test_banner_shows_crawl_progress(engine, session):
    add_champions(session, BLUE + RED)
    import_bootstrap(engine, "16.21", bootstrap_rows())
    with Session(engine) as s:
        s.add(BracketTotal(patch="16.21", bracket="gold", matches=450))
        s.commit()
    note = load_engine_inputs(engine).note
    assert "blended with our crawl" in note and "gold 450" in note and "iron 0" in note
