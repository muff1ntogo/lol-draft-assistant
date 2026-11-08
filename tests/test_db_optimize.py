"""db-optimize (docs/shared-data.md, Stage 1): WITHOUT ROWID for composite-key tables, unused indexes, VACUUM, through
db/session.py, measured on a scratch copy first and never while a crawl writes."""
import hashlib
import json
import os

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session

from draftassist.data.aggregate import aggregate
from draftassist.db.models import Champion
from draftassist.db.optimize import index_usage, optimize_databases, planned_changes
from draftassist.db.session import DROPPED_INDEXES, WITHOUT_ROWID_TABLES, get_engine, init_db, rebuild_table
from test_synergy import BLUE, RED, store
from test_synergy import match as synergy_match


def table_sql(engine, name):
    with engine.connect() as conn:
        return conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}).scalar()


def contents(engine):
    insp = inspect(engine)
    with engine.connect() as conn:
        return {t: sorted(map(tuple, conn.execute(text(f'SELECT * FROM "{t}"')))) for t in insp.get_table_names()}


def filled(path):
    db = init_db(path)
    with Session(db) as s:
        for c in BLUE + RED:
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.commit()
    for i in range(8):
        store(db, synergy_match(f"EUW1_{i}", blue_win=i % 3 == 0, bracket_seed=i), bracket=("gold", "iron")[i % 2])
    aggregate(db, "16.21")
    return db


def make_legacy(path):
    """A database as an older version made it: every table with a rowid, and the keystone index."""
    db = filled(path)
    with db.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
    raw = db.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        for name in WITHOUT_ROWID_TABLES:
            sql, = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            idx = [r[0] for r in cur.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
                                             "AND sql IS NOT NULL", (name,))]
            legacy = sql.replace(f"CREATE TABLE {name}", f"CREATE TABLE _legacy_{name}", 1)
            legacy = legacy[: legacy.upper().rindex("WITHOUT ROWID")].rstrip()
            cur.execute(legacy)
            cur.execute(f'INSERT INTO "_legacy_{name}" SELECT * FROM "{name}"')
            cur.execute(f'DROP TABLE "{name}"')
            cur.execute(f'ALTER TABLE "_legacy_{name}" RENAME TO "{name}"')
            for s in idx:
                cur.execute(s)
        cur.execute("CREATE INDEX ix_match_builds_keystone ON match_builds (keystone)")
        raw.commit()
    finally:
        raw.close()
    db.dispose()
    return get_engine(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fresh_databases_are_created_without_rowid(tmp_path):
    db = init_db(tmp_path / "fresh.sqlite")
    assert {"match_participants", "synergy_pairs", "match_bans", "matchups", "crawled_matches"} <= set(WITHOUT_ROWID_TABLES)
    for name in WITHOUT_ROWID_TABLES:
        assert "WITHOUT ROWID" in table_sql(db, name).upper(), name
    for name in ("drafts", "execution_fits", "match_frames", "match_build_events", "players"):
        assert "WITHOUT ROWID" not in table_sql(db, name).upper(), name  # integer keys, big rows or our own data
    assert planned_changes(db).rebuild == [] and planned_changes(db).drop_indexes == []
    assert {ix["name"] for ix in inspect(db).get_indexes("match_builds")} == set()  # Strategy reads the counts
    assert {"ix_match_builds_keystone", "ix_match_builds_champ_id", "ix_match_build_events_champ_id"} <= set(DROPPED_INDEXES)


def test_rebuild_keeps_every_row_index_and_foreign_key(tmp_path):
    db = make_legacy(tmp_path / "old.sqlite")
    before = contents(db)
    indexes_before = {ix["name"] for ix in inspect(db).get_indexes("crawled_matches")}
    rebuild_table(db, "crawled_matches")  # a foreign-key parent of match_participants and match_bans
    rebuild_table(db, "match_participants")
    assert "WITHOUT ROWID" in table_sql(db, "crawled_matches").upper()
    assert contents(db) == before
    assert {ix["name"] for ix in inspect(db).get_indexes("crawled_matches")} == indexes_before
    with db.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        # the children still point at crawled_matches, and deleting a parent with children is refused
        refs = {r[2] for r in conn.execute(text("PRAGMA foreign_key_list(match_participants)"))}
        assert refs == {"crawled_matches"}


def test_dry_run_measures_on_a_scratch_copy_and_changes_nothing(tmp_path):
    path = tmp_path / "data" / "draftassist.sqlite"
    path.parent.mkdir()
    db = make_legacy(path)
    plan = planned_changes(db)
    assert set(plan.rebuild) == set(WITHOUT_ROWID_TABLES) and plan.drop_indexes == ["ix_match_builds_keystone"]
    db.dispose()
    before = digest(path)
    lines = []
    assert optimize_databases([path], tmp_path / "cache", dry_run=True, log=lines.append) == 0
    assert digest(path) == before
    out = "\n".join(lines)
    assert "match_participants" in out and "ix_match_builds_keystone" in out and "dry run" in out.lower()
    assert "MB" in out  # sizes before and after
    assert not list(tmp_path.rglob("*scratch*"))  # the copy is cleaned up


def test_apply_converts_keeps_data_vacuums_and_is_idempotent(tmp_path):
    path = tmp_path / "draftassist.sqlite"
    db = make_legacy(path)
    before = contents(db)
    db.dispose()
    lines = []
    assert optimize_databases([path], tmp_path / "cache", log=lines.append) == 0
    db = get_engine(path)
    assert contents(db) == before
    for name in WITHOUT_ROWID_TABLES:
        assert "WITHOUT ROWID" in table_sql(db, name).upper()
    assert "ix_match_builds_keystone" not in {ix["name"] for ix in inspect(db).get_indexes("match_builds")}
    with db.connect() as conn:
        assert conn.execute(text("PRAGMA freelist_count")).scalar() == 0  # vacuumed
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    assert planned_changes(db).rebuild == []
    db.dispose()
    lines.clear()
    assert optimize_databases([path], tmp_path / "cache", log=lines.append) == 0
    assert any("nothing to do" in line.lower() for line in lines)


def test_apply_refuses_while_a_crawl_runs(tmp_path):
    path = tmp_path / "draftassist.sqlite"
    make_legacy(path).dispose()
    jobs = tmp_path / "cache" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "running.json").write_text(json.dumps({"job": "crawl", "pid": os.getpid()}), encoding="utf-8")
    before = digest(path)
    lines = []
    assert optimize_databases([path], tmp_path / "cache", log=lines.append) == 1
    assert digest(path) == before
    assert any("crawl" in line.lower() for line in lines)
    # a dry run only reads (through SQLite's backup), so it may run alongside
    assert optimize_databases([path], tmp_path / "cache", dry_run=True, log=lines.append) == 0


def test_a_stale_lock_from_a_finished_crawl_does_not_block(tmp_path):
    path = tmp_path / "draftassist.sqlite"
    make_legacy(path).dispose()
    jobs = tmp_path / "cache" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "running.json").write_text(json.dumps({"job": "crawl", "pid": 2 ** 22 + 12345}), encoding="utf-8")
    assert optimize_databases([path], tmp_path / "cache", log=lambda *_: None) == 0


def test_hot_queries_use_the_kept_indexes(tmp_path):
    from draftassist.db.optimize import query_plans, secondary_indexes

    db = filled(tmp_path / "x.sqlite")
    used = index_usage(db)
    assert set(secondary_indexes(db)) <= set(used)  # every index the models keep serves a hot query
    plans = query_plans(db)
    for name, table in (("strategy builds", "cnt_build"), ("strategy build order", "cnt_order"),
                        ("strategy lane hints", "cnt_lane")):  # a key seek per slice, never a scan of the patch
        line = next(p for p in plans[name] if " c " in f" {p} " or table in p)
        assert "USING PRIMARY KEY" in line and "champ_id=?" in line and "role=?" in line, plans[name]


@pytest.mark.parametrize("name", ["match_participants", "synergy_pairs"])
def test_rebuild_refuses_a_table_with_columns_the_model_lacks(tmp_path, name):
    db = filled(tmp_path / "x.sqlite")
    with db.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{name}" ADD COLUMN legacy_extra INTEGER'))
    with pytest.raises(RuntimeError, match="legacy_extra"):
        rebuild_table(db, name)
    assert "WITHOUT ROWID" in table_sql(db, name).upper()  # untouched
