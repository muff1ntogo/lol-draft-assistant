"""Shared data, Stage 2 (docs/shared-data.md): publish (admin) and pull (users) through a SharedStore, compressed blocks
on Turso (decision 2), the HTTP client (decision 1)."""
import base64
import json
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import Session

from draftassist.data.aggregate import aggregate
from draftassist.data.shared.publish import Quota, publish
from draftassist.data.shared.pull import pull, pull_status
from draftassist.data.shared.schema import IDENTITY_COLUMN, SCHEMA_VERSION, SHARED_TABLES, DERIVED_TABLES, looks_like_identity
from draftassist.data.shared.store import FakeSharedStore, HttpSharedStore, SharedStoreError
from draftassist.db.models import Champion, Player
from draftassist.db.session import init_db
from test_counts import crawl

PUUID = "Xk3v9" * 15 + "abc"  # 78 characters, the shape of a real PUUID


def champions(db):
    with Session(db) as s:
        for c in range(1, 41):
            s.merge(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.commit()


@pytest.fixture
def admin(tmp_path):
    root = tmp_path / "admin"
    db = init_db(root / "draftassist.sqlite")
    crawl(db, n=150)
    with Session(db) as s:  # our own data: must never leave the machine
        s.add(Player(puuid=PUUID, role="mid", riot_id="Secret Name#EUW", rank="GOLD II", bracket="gold"))
        s.commit()
    aggregate(db, "16.20", window="16.21")
    aggregate(db, "16.21")
    master = init_db(root / "master.sqlite")
    crawl(master, n=60, seed=7, brackets=("master",), queues=(420,))
    aggregate(master, "16.20", window="16.21")
    aggregate(master, "16.21")
    return db, master


@pytest.fixture
def user(tmp_path):
    db = init_db(tmp_path / "user" / "draftassist.sqlite")
    champions(db)
    return db


def dump(db, tables):
    with db.connect() as conn:
        have = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))}
        return {t: sorted(map(tuple, conn.execute(text(f'SELECT * FROM "{t}"')))) for t in tables if t in have}


RUNTIME = [*DERIVED_TABLES, *(t.name for t in SHARED_TABLES)]


def master_of(db):
    from draftassist.data.master import master_path

    return init_db(master_path(Path(db.url.database)))


def test_publish_then_pull_reproduces_the_admins_runtime_tables(admin, user):
    main, master = admin
    store = FakeSharedStore()
    result = publish(main, master, store, app_version="0.2.9", log=lambda *_: None)
    assert result.blocks_written > 0 and result.rows_written == result.blocks_written + 1  # + the publish_log row
    status = pull(user, store, log=lambda *_: None)
    assert status.state == "updated" and status.publish_id == result.publish_id
    assert dump(user, RUNTIME) == dump(main, RUNTIME)
    assert dump(master_of(user), RUNTIME) == dump(master, RUNTIME)
    with main.connect() as a, user.connect() as b:  # champions.roles too
        assert a.execute(text("SELECT champ_id, roles FROM champions ORDER BY 1")).all() == \
            b.execute(text("SELECT champ_id, roles FROM champions ORDER BY 1")).all()
    assert pull_status(user)["publish_id"] == result.publish_id


def test_a_second_publish_without_changes_writes_nothing(admin):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    written = store.rows_written
    again = publish(main, master, store, app_version="x", log=lambda *_: None)
    assert again.rows_written == 0 and again.blocks_written == 0 and store.rows_written == written


def test_only_changed_blocks_are_written_and_pulled(admin, user):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    pull(user, store, log=lambda *_: None)
    crawl(main, n=30, seed=99, patches=("16.21",), brackets=("gold",), queues=(440,))  # more gold flex on 16.21
    aggregate(main, "16.21")
    second = publish(main, master, store, app_version="x", log=lambda *_: None)
    assert 0 < second.blocks_written < 40
    status = pull(user, store, log=lambda *_: None)
    assert status.state == "updated" and 0 < status.blocks <= second.blocks_written
    assert dump(user, RUNTIME) == dump(main, RUNTIME)


def test_nothing_identifying_is_published(admin):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    for table in SHARED_TABLES:
        for col in table.columns:
            assert not IDENTITY_COLUMN.search(col), (table.name, col)
    for block in store.decoded_blocks():
        for row in block["rows"]:
            for v in row:
                assert not looks_like_identity(v), (block["table"], v)
    raw = b"".join(store.raw_blocks())
    assert PUUID.encode() not in raw and b"Secret Name" not in raw and b"EUW1_" not in raw  # no match ids either
    assert looks_like_identity(PUUID) and looks_like_identity("Secret Name#EUW") and not looks_like_identity("16.21")


def test_an_interrupted_pull_leaves_local_data_unchanged(admin, user):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    pull(user, store, log=lambda *_: None)
    crawl(main, n=30, seed=99, patches=("16.21",), brackets=("gold",))
    aggregate(main, "16.21")
    publish(main, master, store, app_version="x", log=lambda *_: None)
    before, state = dump(user, RUNTIME), pull_status(user)
    store.fail_reads_after = 1  # the network drops mid-download
    assert pull(user, store, log=lambda *_: None).state == "offline"
    assert dump(user, RUNTIME) == before and pull_status(user) == state
    store.fail_reads_after = None
    store.corrupt_next_block = True  # a block that doesn't match its hash: nothing is applied
    assert pull(user, store, log=lambda *_: None).state == "error"
    assert dump(user, RUNTIME) == before and pull_status(user) == state
    assert pull(user, store, log=lambda *_: None).state == "updated"
    assert dump(user, RUNTIME) == dump(main, RUNTIME)


def test_a_too_new_schema_is_refused_and_local_data_kept(admin, user):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    pull(user, store, log=lambda *_: None)
    before = dump(user, RUNTIME)
    store.execute("UPDATE publish_log SET schema_version = ?", (SCHEMA_VERSION + 1,))
    store.execute("UPDATE shared_blocks SET schema_version = ?, publish_id = publish_id + 100", (SCHEMA_VERSION + 1,))
    store.execute("UPDATE publish_log SET publish_id = publish_id + 100")
    status = pull(user, store, log=lambda *_: None)
    assert status.state == "too_new" and "update" in status.message.lower()
    assert dump(user, RUNTIME) == before


def test_an_interrupted_publish_resumes_with_the_same_publish_id(admin, user):
    main, master = admin
    store = FakeSharedStore(fail_writes_after=1)
    with pytest.raises(SharedStoreError):
        publish(main, master, store, app_version="x", log=lambda *_: None, batch_bytes=20_000)
    assert store.execute("SELECT COUNT(*) FROM publish_log")[0][0] == 0  # not visible to pulls yet
    assert pull(user, store, log=lambda *_: None).state == "unchanged"
    store.fail_writes_after = None
    done = publish(main, master, store, app_version="x", log=lambda *_: None, batch_bytes=20_000)
    assert store.execute("SELECT COUNT(*), MAX(publish_id) FROM publish_log")[0] == (1, done.publish_id)
    assert {r[0] for r in store.execute("SELECT DISTINCT publish_id FROM shared_blocks")} == {done.publish_id}
    pull(user, store, log=lambda *_: None)
    assert dump(user, RUNTIME) == dump(main, RUNTIME)


def test_a_removed_slice_is_tombstoned_and_removed_on_pull(admin, user):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    pull(user, store, log=lambda *_: None)
    with main.begin() as conn:  # the admin drops patch 16.20 (e.g. a reset)
        conn.execute(text("DELETE FROM match_participants WHERE match_id IN "
                          "(SELECT match_id FROM crawled_matches WHERE patch = '16.20')"))
        conn.execute(text("DELETE FROM match_bans WHERE match_id IN (SELECT match_id FROM crawled_matches WHERE patch = '16.20')"))
        conn.execute(text("DELETE FROM match_builds WHERE match_id IN "
                          "(SELECT match_id FROM crawled_matches WHERE patch = '16.20')"))
        conn.execute(text("DELETE FROM crawled_matches WHERE patch = '16.20'"))
    aggregate(main, "16.20", window="16.21")  # empties the patch's counts
    aggregate(main, "16.21")
    second = publish(main, master, store, app_version="x", log=lambda *_: None)
    assert second.tombstones > 0
    pull(user, store, log=lambda *_: None)
    with user.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM cnt_role WHERE patch = '16.20'")).scalar() == 0
    assert dump(user, RUNTIME) == dump(main, RUNTIME)


def test_big_slices_are_split_into_parts_and_reassembled(admin, user):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None, max_block_bytes=4_000)
    assert store.execute("SELECT MAX(parts) FROM shared_blocks")[0][0] > 1
    pull(user, store, log=lambda *_: None)
    assert dump(user, RUNTIME) == dump(main, RUNTIME)
    publish(main, master, store, app_version="x", log=lambda *_: None)  # bigger blocks: the extra parts go
    assert store.execute("SELECT COUNT(*) FROM shared_blocks WHERE deleted = 0 AND part >= parts")[0][0] == 0
    pull(user, store, log=lambda *_: None)
    assert dump(user, RUNTIME) == dump(main, RUNTIME)


def test_the_quota_meter_warns_and_stops(admin):
    main, master = admin
    lines = []
    store = FakeSharedStore()
    stopped = publish(main, master, store, app_version="x", log=lines.append, quota=Quota(monthly_rows=10))
    assert stopped.state == "quota" and stopped.rows_written == 0 and store.rows_written == 0
    assert any("quota" in line.lower() for line in lines)
    lines.clear()
    blocks = publish(main, master, FakeSharedStore(), app_version="x", log=lambda *_: None,
                     quota=Quota(monthly_rows=10**9), dry_run=True).blocks_planned
    warned = publish(main, master, store, app_version="x", log=lines.append,
                     quota=Quota(monthly_rows=int((blocks + 1) / 0.8)))
    assert warned.state == "published" and any("70%" in line or "warning" in line.lower() for line in lines)
    assert "rows written this month" in " ".join(lines).lower()


def test_neither_runs_during_champ_select(admin, user):
    main, master = admin
    store = FakeSharedStore()
    assert publish(main, master, store, app_version="x", in_draft=lambda: True, log=lambda *_: None).state == "draft"
    assert store.rows_written == 0
    publish(main, master, store, app_version="x", log=lambda *_: None)
    assert pull(user, store, in_draft=lambda: True, log=lambda *_: None).state == "draft"
    assert pull_status(user)["publish_id"] is None


def test_an_unreachable_store_keeps_the_last_pull(admin, user):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    pull(user, store, log=lambda *_: None)
    before = dump(user, RUNTIME)
    store.offline = True
    status = pull(user, store, log=lambda *_: None)
    assert status.state == "offline" and dump(user, RUNTIME) == before
    assert pull_status(user)["pulled_at"] is not None  # "Stats from <date>"


def test_live_builds_within_budget_after_a_pull(admin, user, make_services):
    import time

    from draftassist.engine.formats import get_format
    from draftassist.lcu.demo import demo_sessions

    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    pull(user, store, log=lambda *_: None)
    svc = make_services(db=user)
    svc.reload()
    sessions = demo_sessions(svc.live.engine, get_format("ranked"), "blue", game_id=7)
    slow = []
    for s in sessions:
        t = time.perf_counter()
        view = svc.live.view(s)
        if (time.perf_counter() - t) * 1000 > 400:
            slow.append(view["center"]["mode"])
    assert not slow


# ---------- the HTTP store (Hrana over httpx) ----------
def test_http_store_speaks_hrana_and_never_logs_the_token(caplog):
    seen = []

    def handler(req):
        seen.append(req)
        body = json.loads(req.content)
        results = []
        for r in body["requests"]:
            if r["type"] == "close":
                results.append({"type": "ok", "response": {"type": "close"}})
            elif r["type"] == "batch":
                results.append({"type": "ok", "response": {"type": "batch", "result": {
                    "step_results": [{"cols": [], "rows": [], "affected_row_count": 1}] * len(r["batch"]["steps"]),
                    "step_errors": [None] * len(r["batch"]["steps"])}}})
            else:
                results.append({"type": "ok", "response": {"type": "execute", "result": {
                    "cols": [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}],
                    "rows": [[{"type": "integer", "value": "7"}, {"type": "text", "value": "x"},
                              {"type": "blob", "base64": "AAE"}, {"type": "null"}]],  # unpadded, as Turso sends it
                    "affected_row_count": 0}}})
        return httpx.Response(200, json={"baton": None, "base_url": None, "results": results})

    token = "eyJhbGciOiJFZERTQSJ9.secret-token-value.sig"
    store = HttpSharedStore("libsql://team-db.turso.io", token, transport=httpx.MockTransport(handler))
    assert store.execute("SELECT ?, ?, ?, ?", (7, "x", b"\x00\x01", None)) == [(7, "x", b"\x00\x01", None)]
    store.batch([("INSERT INTO t VALUES (?)", (1.5,)), ("INSERT INTO t VALUES (?)", (b"\xff",))])
    assert str(seen[0].url) == "https://team-db.turso.io/v2/pipeline"
    assert seen[0].headers["Authorization"] == f"Bearer {token}"
    stmt = json.loads(seen[0].content)["requests"][0]["stmt"]
    assert stmt["args"] == [{"type": "integer", "value": "7"}, {"type": "text", "value": "x"},
                            {"type": "blob", "base64": "AAE="}, {"type": "null"}]
    steps = json.loads(seen[1].content)["requests"][0]["batch"]["steps"]
    assert steps[0]["stmt"]["sql"] == "BEGIN" and steps[-1]["stmt"]["sql"] == "ROLLBACK"
    assert steps[1]["stmt"]["args"][0] == {"type": "float", "value": 1.5}
    assert token not in repr(store) and token not in caplog.text


def test_http_store_raises_on_errors_and_is_offline_when_unreachable():
    def error(req):
        return httpx.Response(200, json={"results": [{"type": "error", "error": {"message": "no such table: x"}},
                                                     {"type": "ok", "response": {"type": "close"}}]})

    with pytest.raises(SharedStoreError, match="no such table"):
        HttpSharedStore("https://x.turso.io", "t", transport=httpx.MockTransport(error)).execute("SELECT * FROM x")

    def down(req):
        raise httpx.ConnectError("unreachable")

    with pytest.raises(SharedStoreError) as e:
        HttpSharedStore("https://x.turso.io", "t", transport=httpx.MockTransport(down)).execute("SELECT 1")
    assert e.value.offline

    def unauthorised(req):
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(SharedStoreError, match="401"):
        HttpSharedStore("https://x.turso.io", "t", transport=httpx.MockTransport(unauthorised)).execute("SELECT 1")


def test_only_the_sync_layer_talks_to_turso():
    src = Path(__file__).resolve().parents[1] / "src" / "draftassist"
    offenders = []
    for p in src.rglob("*.py"):
        if "shared" in p.relative_to(src).parts:
            continue
        s = p.read_text(encoding="utf-8")
        if re.search(r"v2/pipeline|turso\.io|HttpSharedStore|from draftassist\.data\.shared\.store", s):
            offenders.append(str(p.relative_to(src)))
    assert offenders == []


# ---------- in the app ----------
def test_sync_now_pulls_and_reloads_and_reports_the_date(admin, user, make_services):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    main, master = admin
    store = FakeSharedStore()
    done = publish(main, master, store, app_version="x", log=lambda *_: None)
    svc = make_services(db=user)
    svc.shared_store = store
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        before = client.get("/api/shared/status").json()
        assert before["configured"] and before["pull"]["publish_id"] is None
        after = client.post("/api/shared/sync").json()
    assert after["last"]["state"] == "updated" and after["pull"]["publish_id"] == done.publish_id
    assert after["pull"]["published_at"] is not None
    assert svc.loaded_version is not None  # the stats were reloaded


def test_the_app_never_pulls_in_a_draft_or_over_its_own_crawl(admin, user, make_services):
    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="x", log=lambda *_: None)
    svc = make_services(db=user)
    svc.shared_store = store
    svc.in_draft = lambda: True
    assert svc.sync_shared()["state"] == "draft"
    crawler = make_services(db=main)  # the admin's own machine
    crawler.shared_store = store
    assert crawler.sync_shared()["state"] == "local"
    plain = make_services(db=user)
    assert plain.sync_shared()["state"] == "off"  # nothing set up: solo mode as today


def test_shared_inspect_decodes_what_is_published(admin):
    from draftassist.data.shared.published import inspect_store

    main, master = admin
    store = FakeSharedStore()
    publish(main, master, store, app_version="0.2.9", log=lambda *_: None)
    lines = []
    out = inspect_store(store, table="cnt_role", patch="16.21", rows=3, log=lines.append)
    assert out["publishes"] == 1 and out["tables"]["main|cnt_role"][0] > 0
    text_out = "\n".join(lines)
    assert "app 0.2.9" in text_out and "cnt_role ['16.21'" in text_out and "champ_id" in text_out


def test_publish_counts_patches_crawled_before_the_count_tables(tmp_path):
    from draftassist.data.shared.publish import ensure_counts

    db = init_db(tmp_path / "old.sqlite")
    crawl(db, n=40, patches=("16.21",))
    with db.begin() as conn:  # as an older version left it: per-match rows, no counts
        conn.execute(text("DELETE FROM cnt_bracket"))
    assert ensure_counts(db, log=lambda *_: None) == ["16.21"]
    assert ensure_counts(db, log=lambda *_: None) == []
