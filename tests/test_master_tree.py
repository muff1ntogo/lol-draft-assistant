"""Master+ for the Matchup tree alone: its own crawl and database, and nothing of it reaching the main database."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from test_data_pipeline import BLUE, RED, FakeClock, FakeRiot, make_match

from draftassist.api.main import create_app
from draftassist.config import Settings
from draftassist.data import crawl, master
from draftassist.data.aggregate import aggregate
from draftassist.data.crawler import MASTER_PLUS, Crawler
from draftassist.data.master import master_matches, master_path, prepare_master, usable_master
from draftassist.data.riot import RateLimiter, RiotClient
from draftassist.db.models import Champion, CrawledMatch, SeedPlayer
from draftassist.db.session import init_db


class FakeLadder(FakeRiot):
    """FakeRiot plus the apex ladders, which have no divisions or pages."""

    def __init__(self, matches_by_puuid, ladders):
        super().__init__(matches_by_puuid)
        self.ladders = ladders

    def __call__(self, req):
        for tier, puuids in self.ladders.items():
            if f"/league/v4/{tier}leagues/" in req.url.path:
                self.calls.append(req.url.path)
                return httpx.Response(200, json={"tier": tier.upper(), "entries": [{"puuid": p, "rank": "I"} for p in puuids]})
        return super().__call__(req)


def riot_client(tmp_path, handler):
    clock = FakeClock()
    return RiotClient("k", tmp_path, limiter=RateLimiter(clock=clock, sleep=clock.sleep),
                      transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock)


def test_the_apex_ladders_seed_one_bracket_keeping_each_players_tier(engine, tmp_path):
    fake = FakeLadder({}, {"challenger": ["c1"], "grandmaster": ["g1"], "master": ["m1", "c1"]})
    added = Crawler(riot_client(tmp_path, fake), engine, "16.21", brackets=(MASTER_PLUS,)).seed()
    assert added == 3
    with Session(engine) as s:
        seeds = {p.puuid: p for p in s.exec(select(SeedPlayer)).all()}
    assert {p: seeds[p].tier for p in seeds} == {"c1": "CHALLENGER", "g1": "GRANDMASTER", "m1": "MASTER"}
    assert {p.bracket for p in seeds.values()} == {MASTER_PLUS}
    assert not any("/entries/" in c for c in fake.calls)  # the divisioned endpoint doesn't serve these tiers


def _main_with_champions(path, patch="16.22.1"):
    main = init_db(path)
    with Session(main) as s:
        for c in (*BLUE, *RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch=patch))
        s.commit()
    return main


def test_the_crawl_fills_its_own_database_and_leaves_the_main_one_alone(tmp_path, monkeypatch):
    main_path = tmp_path / "main.sqlite"
    main = _main_with_champions(main_path)
    fake = FakeLadder({"p": [make_match("EUW1_21", BLUE, RED, version="16.21.1.1"),
                             make_match("EUW1_22", BLUE, RED, version="16.22.1.1"),
                             make_match("EUW1_20", BLUE, RED, version="16.20.1.1"),
                             make_match("EUW1_flex", BLUE, RED, version="16.22.1.1", queue=440)]},
                      {"challenger": ["p"], "grandmaster": [], "master": []})
    monkeypatch.setattr(crawl, "load_settings", lambda: Settings(riot_api_key="k", db_path=main_path, cache_dir=tmp_path))
    monkeypatch.setattr(crawl, "RiotClient", lambda key, cache: riot_client(tmp_path, fake))
    assert crawl.main(["--master", "--no-timelines", "--max-matches", "10"]) == 0

    with Session(main) as s:
        assert s.exec(select(CrawledMatch)).all() == [] and s.exec(select(SeedPlayer)).all() == []
    db = init_db(master_path(main_path))
    with Session(db) as s:
        stored = {m.match_id: (m.patch, m.bracket) for m in s.exec(select(CrawledMatch)).all()}
    # Both patches on one run; the older patch is left out and Flex is never asked for
    assert stored == {"EUW1_21": ("16.21", MASTER_PLUS), "EUW1_22": ("16.22", MASTER_PLUS)}
    assert master_matches(main) == 2
    with db.connect() as conn:
        patches = {r[0] for r in conn.exec_driver_sql("SELECT DISTINCT patch FROM bracket_totals")}
    assert patches == {"16.21", "16.22"}  # both aggregated


def test_master_refuses_a_patch_or_brackets(capsys):
    for extra in (["--patch", "16.21"], ["--brackets", "gold"]):
        with pytest.raises(SystemExit):
            crawl.main(["--master", "--max-matches", "1", *extra])


def test_an_ordinary_crawl_still_keeps_one_patch_and_both_queues(engine, tmp_path):
    with Session(engine) as s:
        for c in (*BLUE, *RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.add(SeedPlayer(puuid="p", bracket="gold", tier="GOLD", division="I"))
        s.commit()
    fake = FakeRiot({"p": [make_match("EUW1_21", BLUE, RED, version="16.21.1.1"),
                           make_match("EUW1_22", BLUE, RED, version="16.22.1.1"),
                           make_match("EUW1_flex", BLUE, RED, version="16.21.1.1", queue=440)]})
    stats = Crawler(riot_client(tmp_path, fake), engine, "16.21", brackets=("gold",), clock=lambda: 1_793_000_000).run(max_matches=10)
    assert stats.stored == {"gold": 2} and stats.skipped_other_patch == 1


# ---------- the tree ----------
def _master_with_games(main, games=300):
    from test_lane_matchup import FILLER_BLUE, FILLER_RED, _store

    with Session(main) as s:
        for c in (1, 3, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.22.1", tags=["Fighter"]))
        s.commit()
    path = master_path(main.url.database)
    db = prepare_master(main, path)
    with Session(db) as s:
        for i in range(games):
            _store(s, f"EUW1_m{i}", (1, *FILLER_BLUE), (3, *FILLER_RED), i % 5 != 0, patch="16.21", bracket=MASTER_PLUS)
        s.commit()
    aggregate(db, "16.21", window="16.22")
    aggregate(db, "16.22", window="16.22")
    return db


def _tree(make_services, main):
    with TestClient(create_app(make_services(db=main), stats_refresh_s=0)) as c:
        return c.get("/api/prep/matchup-tree").json()


def test_before_any_crawl_the_tree_says_it_is_on_the_main_data(make_services, engine):
    body = _tree(make_services, engine)
    assert body["master"] is False and body["master_matches"] == 0
    assert body["master_needed"] == master.MIN_MATCHES


def test_too_few_games_keep_the_tree_on_the_main_data_and_say_how_many(make_services, engine):
    _master_with_games(engine, games=20)
    assert usable_master(engine) is None
    body = _tree(make_services, engine)
    assert body["master"] is False and body["master_matches"] == 20
    assert body["matches"] == []  # the main database's own counts


def test_with_enough_games_the_tree_reads_master_only(make_services, engine, monkeypatch):
    _master_with_games(engine)
    monkeypatch.setattr(master, "MIN_MATCHES", 300)
    body = _tree(make_services, engine)
    assert body["master"] is True and body["master_matches"] == 300
    assert [(m["patch"], m["matches"]) for m in body["matches"]] == [("16.21", 300)]
    with Session(engine) as s:
        assert s.exec(select(CrawledMatch)).all() == []  # still nothing in the main database


def test_the_tree_uses_the_2v2_tables_it_is_given():
    from dataclasses import replace

    from draftassist.engine.engine import DraftEngine
    from draftassist.engine.simulate import synthetic_data

    data, team = synthetic_data()
    eng = DraftEngine(replace(data, bot=None), team)
    base = object()
    assert eng.resp_data(frozenset(), base=base) is base  # no tables anywhere: the 1v1s alone
    given = object()
    wrapped = eng.resp_data(frozenset(), base=base, bot=given)
    assert wrapped is not base and wrapped._bot is given
