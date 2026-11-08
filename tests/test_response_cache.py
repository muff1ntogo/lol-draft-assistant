"""The packed Riot response cache (docs/shared-data.md, Stage 1): cache.sqlite, legacy .json files, packing, archives."""
import json
import random
import sqlite3

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import Session

from draftassist.data.backfill import backfill_damage, backfill_durations, backfill_traits
from draftassist.data.builds import backfill_builds
from draftassist.data.cache_archive import archive_cache
from draftassist.data.cache_pack import PackError, pack_cache
from draftassist.data.response_cache import (
    CACHE_FILE,
    ResponseCache,
    cached_match,
    canonical,
    close_all,
    decode,
    encode,
    has_match,
    open_cache,
    request_key,
)
from draftassist.data.riot import REGION, RateLimiter, RiotClient, cache_path, match_path
from draftassist.data.timelines import estimate_refetch
from draftassist.db.models import Champion
from draftassist.db.session import init_db
from test_data_pipeline import FakeClock
from test_synergy import BLUE, RED, store
from test_synergy import match as synergy_match

KEY = "RGAPI-00000000-1111-2222-3333-444444444444"


@pytest.fixture(autouse=True)
def _close_caches():
    yield
    close_all()  # Windows keeps open files: let tmp_path go


def body(i: int, name: str = "Ahri") -> dict:
    """A body shaped like a match (nested, repetitive, unicode, floats, nulls)."""
    return {"metadata": {"matchId": f"EUW1_{i}"},
            "info": {"gameVersion": "16.21.712.4501", "queueId": 420, "gameDuration": 1800 + i,
                     "participants": [{"championId": c, "championName": name, "riotIdGameName": "Ünïcødé 名前",
                                       "challenges": {"kda": 3.25 + c / 7, "x": None}, "win": c < 6}
                                      for c in range(1, 11)]}}


def write_legacy(cache_dir, host, path, params, entry_body, fetched_at=100.0, key_id="abc123abc123"):
    p = cache_path(cache_dir / "riot", host, path, params)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"fetched_at": fetched_at, "body": entry_body}
    if key_id is not None:
        entry["key"] = key_id
    p.write_text(json.dumps(entry), encoding="utf-8")
    return p


def riot(cache_dir, handler):
    clock = FakeClock()
    return RiotClient(KEY, cache_dir, limiter=RateLimiter(clock=clock, sleep=clock.sleep),
                      transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock)


# ---------- codecs ----------
@pytest.mark.parametrize("codec", ["json", "gzip", "zstd"])
def test_codecs_round_trip_byte_identical_json(codec):
    raw = canonical(body(1))
    name, blob = encode(raw, codec=codec)
    assert name == codec
    assert decode(name, blob, lambda _id: None) == raw
    if codec != "json":
        assert len(blob) < len(raw)


def test_canonical_json_is_stable_and_compact():
    b = body(3)
    assert canonical(b) == canonical(json.loads(json.dumps(b)))  # through a .json file and back
    assert b" " not in canonical({"a": [1, 2]})
    assert canonical(None) == b"null"


def test_request_key_is_the_legacy_file_name():
    p = cache_path(__import__("pathlib").Path("x"), "europe", "/lol/match/v5/matches/EUW1_1", {"b": 2, "a": 1})
    assert p.stem == request_key("europe", "/lol/match/v5/matches/EUW1_1", {"a": 1, "b": 2})


# ---------- ResponseCache ----------
def test_put_get_round_trip_keeps_fetched_at_and_key_id(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("europe", "/m/1", None, body(1), fetched_at=123.5, key_id="k1")
    entry = cache.get("europe", "/m/1", None)
    assert entry.body == body(1) and entry.fetched_at == 123.5 and entry.key_id == "k1"
    assert cache.get("europe", "/m/2", None) is None
    cache.put("euw1", "/null", None, None, fetched_at=1.0, key_id="k1")  # a cached 404
    assert cache.get("euw1", "/null", None).body is None


def test_reading_never_creates_the_cache_file(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.get("europe", "/m/1", None) is None
    assert not (tmp_path / CACHE_FILE).exists()


def test_table_is_without_rowid_and_codec_is_per_row(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.put("europe", "/a", None, body(1), 1.0, "k")
    cache.close()
    conn = sqlite3.connect(tmp_path / CACHE_FILE)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'responses'").fetchone()[0]
    assert "WITHOUT ROWID" in sql.upper()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(responses)")]
    assert cols == ["key", "host", "fetched_at", "key_id", "codec", "body"]
    # rows written with other codecs (an older version, another machine) read side by side
    for i, codec in ((2, "gzip"), (3, "json")):
        name, blob = encode(canonical(body(i)), codec=codec)
        conn.execute("INSERT INTO responses VALUES (?, ?, ?, ?, ?, ?)",
                     (request_key("europe", f"/m/{i}", None), "europe", 2.0, "k", name, blob))
    conn.commit()
    conn.close()
    cache = ResponseCache(tmp_path)
    assert cache.get("europe", "/a", None).body == body(1)
    assert cache.get("europe", "/m/2", None).body == body(2)
    assert cache.get("europe", "/m/3", None).body == body(3)


def test_legacy_file_is_read_when_there_is_no_row(tmp_path):
    write_legacy(tmp_path, "europe", "/m/1", None, body(1), fetched_at=50.0, key_id="old")
    entry = ResponseCache(tmp_path).get("europe", "/m/1", None)
    assert entry.body == body(1) and entry.fetched_at == 50.0 and entry.key_id == "old"
    write_legacy(tmp_path, "europe", "/m/2", None, body(2), key_id=None)  # written before key fingerprints
    assert ResponseCache(tmp_path).get("europe", "/m/2", None).key_id is None


def test_packed_row_wins_over_a_stale_legacy_file(tmp_path):
    write_legacy(tmp_path, "europe", "/m/1", None, {"old": True}, fetched_at=1.0)
    cache = ResponseCache(tmp_path)
    cache.put("europe", "/m/1", None, {"new": True}, fetched_at=2.0, key_id="k")
    assert cache.get("europe", "/m/1", None).body == {"new": True}


def test_dictionary_codec_once_trained(tmp_path):
    cache = ResponseCache(tmp_path)
    assert cache.train_dictionary([canonical(body(i, name=f"N{i % 7}")) for i in range(300)]) == 1
    cache.put("europe", "/m/1", None, body(1), 1.0, "k")
    cache.close()
    conn = sqlite3.connect(tmp_path / CACHE_FILE)
    assert conn.execute("SELECT codec FROM responses").fetchone()[0] == "zstd-d1"
    conn.close()
    assert ResponseCache(tmp_path).get("europe", "/m/1", None).body == body(1)  # a new reader loads the dictionary


# ---------- RiotClient through the cache ----------
def test_riot_client_writes_rows_not_files_and_never_stores_the_key(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"n": len(calls)})

    c = riot(tmp_path, handler)
    assert c.get("europe", "/x", {"a": 1}) == {"n": 1}
    assert c.get("europe", "/x", {"a": 1}) == {"n": 1}
    assert len(calls) == 1
    assert not list((tmp_path / "riot").rglob("*.json"))
    close_all()
    assert KEY.encode() not in (tmp_path / CACHE_FILE).read_bytes()


def test_riot_client_reads_a_mixed_cache_during_migration(tmp_path):
    """Half the cache still in files, half packed: every entry is a hit, no request is made."""
    c0 = riot(tmp_path, lambda req: httpx.Response(200, json={}))
    for i in range(4):
        write_legacy(tmp_path, "europe", f"/m/{i}", None, body(i), key_id=c0.key_id)
    pack_cache(tmp_path, limit=2, log=lambda *_: None)  # stopped after two files
    assert len(list((tmp_path / "riot").rglob("*.json"))) == 2

    def fail(req):
        raise AssertionError(f"unexpected request {req.url}")

    c = riot(tmp_path, fail)
    assert [c.get("europe", f"/m/{i}") for i in range(4)] == [body(i) for i in range(4)]


def test_riot_client_ttl_and_key_rules_hold_for_packed_rows(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=["fresh"])

    c = riot(tmp_path, handler)
    open_cache(tmp_path).put("euw1", "/ladder", None, ["stale"], fetched_at=c._clock() - 10_000, key_id=c.key_id)
    assert c.get("euw1", "/ladder", ttl=60) == ["fresh"]  # too old
    open_cache(tmp_path).put("euw1", "/other", None, ["theirs"], fetched_at=c._clock(), key_id="another-key")
    assert c.get("euw1", "/other") == ["fresh"]  # another key's entry is a miss
    assert len(calls) == 2


# ---------- match helpers (backfill, refetch estimate) ----------
def test_cached_match_reads_rows_files_and_archives(tmp_path):
    write_legacy(tmp_path, REGION, match_path("EUW1_1"), None, body(1))
    open_cache(tmp_path).put(REGION, match_path("EUW1_2"), None, body(2), 1.0, "k")
    assert cached_match(tmp_path, "EUW1_1") == body(1)
    assert cached_match(tmp_path, "EUW1_2") == body(2)
    assert cached_match(tmp_path, "EUW1_3") is None
    assert has_match(tmp_path, "EUW1_1") and has_match(tmp_path, "EUW1_2") and not has_match(tmp_path, "EUW1_3")


def test_refetch_estimate_counts_packed_matches_as_cached(tmp_path):
    open_cache(tmp_path).put(REGION, match_path("EUW1_1"), None, body(1), 1.0, "k")
    write_legacy(tmp_path, REGION, match_path("EUW1_2"), None, body(2))
    est = estimate_refetch(["EUW1_1", "EUW1_2", "EUW1_3"], tmp_path)
    assert est["requests"] == 3 + 1  # three timelines, one match body


# ---------- cache-pack ----------
def legacy_cache(cache_dir, n=30, seed=1):
    rnd = random.Random(seed)
    written = {}
    for i in range(n):
        host = rnd.choice(["europe", "euw1"])
        path, params = f"/thing/{i}", ({"page": i} if i % 3 else None)
        b = body(i) if i % 4 else [f"EUW1_{i}", f"EUW1_{i + 1}"]
        write_legacy(cache_dir, host, path, params, b, fetched_at=1000.0 + i, key_id="kid" if i % 5 else None)
        written[(host, path, json.dumps(params))] = (b, 1000.0 + i, "kid" if i % 5 else None)
    return written


def assert_all_readable(cache_dir, written):
    cache = ResponseCache(cache_dir)
    for (host, path, params), (b, fetched_at, key_id) in written.items():
        e = cache.get(host, path, json.loads(params))
        assert e is not None, (host, path)
        assert canonical(e.body) == canonical(b) and e.fetched_at == fetched_at and e.key_id == key_id


def test_pack_converts_verifies_and_deletes(tmp_path):
    written = legacy_cache(tmp_path)
    lines = []
    result = pack_cache(tmp_path, batch_size=7, log=lines.append)
    assert result.packed == 30 and result.deleted == 30 and result.unreadable == 0
    assert not list((tmp_path / "riot").rglob("*.json"))
    assert_all_readable(tmp_path, written)
    assert result.before_bytes > 0 and result.after_bytes > 0
    assert any("before" in line and "after" in line for line in lines)


def test_pack_keep_leaves_files_and_a_second_run_writes_nothing(tmp_path):
    written = legacy_cache(tmp_path)
    first = pack_cache(tmp_path, keep=True, log=lambda *_: None)
    assert first.packed == 30 and first.deleted == 0
    assert len(list((tmp_path / "riot").rglob("*.json"))) == 30
    second = pack_cache(tmp_path, keep=True, log=lambda *_: None)
    assert second.packed == 0 and second.already == 30
    assert_all_readable(tmp_path, written)


def test_pack_trains_a_dictionary_from_a_big_enough_cache(tmp_path):
    for i in range(250):
        write_legacy(tmp_path, REGION, match_path(f"EUW1_{i}"), None, body(i, name=f"N{i % 9}"))
    result = pack_cache(tmp_path, log=lambda *_: None)
    assert result.dictionary == 1
    conn = sqlite3.connect(tmp_path / CACHE_FILE)
    assert {r[0] for r in conn.execute("SELECT DISTINCT codec FROM responses")} == {"zstd-d1"}
    conn.close()


def test_pack_leaves_unreadable_files_in_place(tmp_path):
    legacy_cache(tmp_path, n=5)
    bad = tmp_path / "riot" / "europe" / ("0" * 40 + ".json")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    result = pack_cache(tmp_path, log=lambda *_: None)
    assert result.unreadable == 1 and bad.is_file() and result.packed == 5


def test_pack_keeps_a_newer_row_and_drops_the_stale_file(tmp_path):
    p = write_legacy(tmp_path, "euw1", "/ladder", None, ["old"], fetched_at=1.0)
    open_cache(tmp_path).put("euw1", "/ladder", None, ["new"], fetched_at=5.0, key_id="k")
    result = pack_cache(tmp_path, log=lambda *_: None)
    assert result.already == 1 and not p.exists()
    assert ResponseCache(tmp_path).get("euw1", "/ladder", None).body == ["new"]


class Boom(RuntimeError):
    pass


@pytest.mark.parametrize("stage", ["inserting", "committed"])
def test_a_crash_mid_pack_loses_nothing_and_a_rerun_finishes(tmp_path, stage):
    """Killed halfway through a batch (before its commit) or between the commit and deleting the files."""
    written = legacy_cache(tmp_path, n=25)
    seen = []

    def crash(event, batch):
        seen.append((event, batch))
        if event == stage and batch == 1:
            raise Boom()

    with pytest.raises(Boom):
        pack_cache(tmp_path, batch_size=10, on_event=crash, log=lambda *_: None)
    assert_all_readable(tmp_path, written)  # every entry is still somewhere: a file, a row or both
    close_all()
    result = pack_cache(tmp_path, batch_size=10, log=lambda *_: None)
    assert result.packed + result.already == 15  # batch 0 was finished; batches 1 and 2 are left (rows or files)
    assert result.already == (10 if stage == "committed" else 0)
    assert not list((tmp_path / "riot").rglob("*.json"))
    assert_all_readable(tmp_path, written)


def test_pack_refuses_to_delete_when_a_row_does_not_verify(tmp_path, monkeypatch):
    legacy_cache(tmp_path, n=3)
    import draftassist.data.cache_pack as cp

    monkeypatch.setattr(cp, "canonical", lambda b: b"different")  # what reads back never matches
    with pytest.raises(PackError):
        pack_cache(tmp_path, log=lambda *_: None)
    assert len(list((tmp_path / "riot").rglob("*.json"))) == 3


# ---------- cache-archive ----------
def test_archive_moves_old_patches_and_reads_still_find_them(tmp_path):
    cache = open_cache(tmp_path)
    patches = ["15.24", "16.1", "16.2", "16.3"]
    for i, patch in enumerate(patches):
        b = body(i)
        b["info"]["gameVersion"] = f"{patch}.700.1"
        cache.put(REGION, match_path(f"EUW1_{i}"), None, b, 1.0, "k")
    cache.put(REGION, "/lol/match/v5/matches/by-puuid/x/ids", {"count": 20}, ["EUW1_0"], 1.0, "k")  # not a match: stays
    moved = archive_cache(tmp_path, older_than_patches=2, log=lambda *_: None)
    assert moved == {"15.24": 1, "16.1": 1}
    assert (tmp_path / "cache-archive-15.24.sqlite").is_file() and (tmp_path / "cache-archive-16.1.sqlite").is_file()
    close_all()
    conn = sqlite3.connect(tmp_path / CACHE_FILE)
    assert conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 3
    conn.close()
    for i in range(4):
        assert cached_match(tmp_path, f"EUW1_{i}")["metadata"]["matchId"] == f"EUW1_{i}"
    assert archive_cache(tmp_path, older_than_patches=2, log=lambda *_: None) == {}


def test_archive_is_self_contained_with_its_dictionary(tmp_path):
    cache = open_cache(tmp_path)
    cache.train_dictionary([canonical(body(i, name=f"N{i % 5}")) for i in range(300)])
    old = body(1)
    old["info"]["gameVersion"] = "16.1.1"
    new = body(2)
    new["info"]["gameVersion"] = "16.9.1"
    cache.put(REGION, match_path("EUW1_1"), None, old, 1.0, "k")
    cache.put(REGION, match_path("EUW1_2"), None, new, 1.0, "k")
    archive_cache(tmp_path, older_than_patches=1, log=lambda *_: None)
    close_all()
    (tmp_path / CACHE_FILE).unlink()  # the archive alone must still decode
    for suffix in ("-wal", "-shm"):
        (tmp_path / (CACHE_FILE + suffix)).unlink(missing_ok=True)
    assert cached_match(tmp_path, "EUW1_1") == old


# ---------- backfill gives the same result from a packed cache ----------
def _crawl_db(path):
    db = init_db(path)
    with Session(db) as s:
        for c in BLUE + RED:
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.commit()
    raws = [synergy_match(f"EUW1_{i}", blue_win=i % 2 == 0, bracket_seed=i) for i in range(6)]
    for raw in raws:
        store(db, raw)
    with db.begin() as conn:  # as if stored before these columns existed
        conn.execute(text("UPDATE match_participants SET dmg_phys = NULL, dmg_magic = NULL, dmg_true = NULL, cc_time = NULL"))
        conn.execute(text("UPDATE crawled_matches SET game_duration_s = NULL"))
        conn.execute(text("DELETE FROM match_builds"))
    return db, raws


def _dump(db):
    with db.connect() as conn:
        return {t: sorted(map(tuple, conn.execute(text(f"SELECT * FROM {t}"))))
                for t in ("match_participants", "crawled_matches", "match_builds")}


def _backfill(db, cache_dir):
    return [backfill_damage(db, cache_dir), backfill_durations(db, cache_dir), backfill_traits(db, cache_dir),
            backfill_builds(db, cache_dir)]


def test_backfill_is_identical_from_files_and_from_a_packed_cache(tmp_path):
    files_dir, packed_dir = tmp_path / "files", tmp_path / "packed"
    db_a, raws = _crawl_db(tmp_path / "a.sqlite")
    db_b, _ = _crawl_db(tmp_path / "b.sqlite")
    for d in (files_dir, packed_dir):
        for raw in raws[:5]:  # one match missing from the cache in both
            write_legacy(d, REGION, match_path(raw["metadata"]["matchId"]), None, raw)
    pack_cache(packed_dir, log=lambda *_: None)
    assert not list((packed_dir / "riot").rglob("*.json"))
    assert _backfill(db_a, files_dir) == _backfill(db_b, packed_dir)
    assert _dump(db_a) == _dump(db_b)
