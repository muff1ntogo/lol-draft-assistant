"""Switching Riot API keys (e.g. development key -> approved app key). Riot encrypts PUUIDs per app, so
PUUIDs fetched with one key are rejected (400) by another: cached responses carrying them, ladder seeds
and our players' API PUUIDs must not survive a key change."""
import httpx
from sqlmodel import Session, select
from test_data_pipeline import BLUE, RED, FakeClock, FakeRiot, add_champions, make_match

from draftassist.data.crawler import Crawler
from draftassist.data.pools import backfill_player_ids
from draftassist.data.riot import RateLimiter, RiotClient, key_id, sync_api_key
from draftassist.db.models import CrawledMatch, Player, SeedPlayer
from draftassist.learning.history import fetch_history

KEY_A, KEY_B = "RGAPI-dev-key-aaaa", "RGAPI-app-key-bbbb"


def client(tmp_path, handler, key):
    clock = FakeClock()
    return RiotClient(key, tmp_path, limiter=RateLimiter(clock=clock, sleep=clock.sleep),
                      transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock)


def test_key_id_is_a_short_one_way_fingerprint():
    assert key_id(KEY_A) == key_id(KEY_A) != key_id(KEY_B)
    assert len(key_id(KEY_A)) == 12 and "RGAPI" not in key_id(KEY_A)


def test_cache_from_another_key_is_a_miss(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.headers["X-Riot-Token"])
        return httpx.Response(200, json=[req.headers["X-Riot-Token"][-4:]])

    assert client(tmp_path, handler, KEY_A).get("euw1", "/ladder") == ["aaaa"]
    assert client(tmp_path, handler, KEY_A).get("euw1", "/ladder") == ["aaaa"]  # cached
    assert client(tmp_path, handler, KEY_B).get("euw1", "/ladder") == ["bbbb"]  # other key: refetched
    assert calls == [KEY_A, KEY_B]
    from draftassist.data.response_cache import CACHE_FILE, close_all

    close_all()
    stored = (tmp_path / CACHE_FILE).read_bytes()
    assert not any(k.encode() in stored for k in (KEY_A, KEY_B))


def test_cache_entries_written_before_fingerprints_are_misses(tmp_path):
    c = client(tmp_path, lambda req: httpx.Response(200, json=["new"]), KEY_B)
    path = c._cache_path("euw1", "/ladder", None)
    path.parent.mkdir(parents=True)
    path.write_text('{"fetched_at": 0, "body": ["old"]}', encoding="utf-8")
    assert c.get("euw1", "/ladder") == ["new"]


def test_key_change_resets_seeds_and_api_puuids(engine):
    with Session(engine) as s:
        s.add(SeedPlayer(puuid="old-seed", bracket="gold", tier="GOLD", division="I"))
        s.add(Player(puuid="internal-1", riot_id="Me#EUW", role="top", rank="", bracket="gold",
                     client_puuid="client-uuid", api_puuid="old-api-puuid"))
        s.commit()
    assert sync_api_key(engine, KEY_A) is True  # first key ever recorded: unknown origin, reset
    with Session(engine) as s:
        assert s.exec(select(SeedPlayer)).all() == []
        me = s.get(Player, "internal-1")
        assert me.api_puuid is None and me.client_puuid == "client-uuid"  # the client's PUUID is unaffected
        s.add(SeedPlayer(puuid="seed-a", bracket="gold", tier="GOLD", division="I"))
        s.commit()
    assert sync_api_key(engine, KEY_A) is False  # same key: nothing touched
    with Session(engine) as s:
        assert len(s.exec(select(SeedPlayer)).all()) == 1
    assert sync_api_key(engine, KEY_B) is True
    with Session(engine) as s:
        assert s.exec(select(SeedPlayer)).all() == []


def test_crawler_skips_a_player_the_api_rejects(engine, session, tmp_path):
    add_champions(session, BLUE + RED)
    fake = FakeRiot({"good": [make_match("EUW1_50", BLUE, RED)]})

    def handler(req):
        if "/by-puuid/bad/" in req.url.path:
            return httpx.Response(400, json={"status": {"message": "Exception decrypting bad"}})
        return fake(req)

    with Session(engine) as s:
        s.add(SeedPlayer(puuid="bad", bracket="gold", tier="GOLD", division="I"))
        s.add(SeedPlayer(puuid="good", bracket="gold", tier="GOLD", division="II"))
        s.commit()
    crawler = Crawler(client(tmp_path, handler, KEY_A), engine, "16.21", brackets=("gold",),
                      clock=lambda: 1_793_000_000)
    stats = crawler.run(max_matches=10)
    assert stats.stored == {"gold": 1} and stats.players_rejected == 1
    with Session(engine) as s:
        assert [m.match_id for m in s.exec(select(CrawledMatch)).all()] == ["EUW1_50"]


# ---------- our players after a key change ----------
OLD_API, NEW_API = "o" * 78, "n" * 78  # the same account's PUUID under the old and the new key


def test_startup_migration_does_not_restore_an_old_api_puuid(engine):
    # a player linked under the old key keeps that PUUID as their internal key
    with Session(engine) as s:
        s.add(Player(puuid=OLD_API, riot_id="Mid#EUW", role="mid", rank="", bracket="gold", api_puuid=OLD_API))
        s.commit()
    sync_api_key(engine, KEY_B)
    backfill_player_ids(engine)
    with Session(engine) as s:
        assert s.get(Player, OLD_API).api_puuid is None  # re-resolved from the Riot ID instead


def test_history_re_resolves_a_rejected_api_puuid(engine, tmp_path):
    with Session(engine) as s:
        s.add(Player(puuid=OLD_API, riot_id="Mid#EUW", role="mid", rank="", bracket="gold", api_puuid=OLD_API))
        s.commit()
    seen = []

    def handler(req):
        path = req.url.path
        seen.append(path)
        if path.endswith("/accounts/by-riot-id/Mid/EUW"):
            return httpx.Response(200, json={"puuid": NEW_API, "gameName": "Mid", "tagLine": "EUW"})
        if f"/by-puuid/{OLD_API}/" in path:
            return httpx.Response(400, json={"status": {"message": "Exception decrypting"}})
        if f"/by-puuid/{NEW_API}/ids" in path:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    added = fetch_history(engine, client(tmp_path, handler, KEY_B), now=1_793_000_000)
    assert added == {"mid": 0}  # fetched with the new PUUID, no error
    with Session(engine) as s:
        assert s.get(Player, OLD_API).api_puuid == NEW_API
    assert any(NEW_API in p for p in seen)
