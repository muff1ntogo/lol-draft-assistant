"""Smaller databases (docs/shared-data.md, Stage 1): WITHOUT ROWID for the text- and composite-keyed tables, the unused
indexes dropped, then VACUUM. Through db/session.py's migration helpers.

    draftassist db-optimize --dry-run    measure every change on a scratch copy (dbstat), change nothing
    draftassist db-optimize              apply them: the main database and, if there is one, master.sqlite

Applying refuses while a job runs (cache/jobs/running.json): a crawl writing during a table rebuild would wait on it, and
the rebuild of match_participants takes a while. Close the app too, so nothing reads mid-rebuild. A dry run only reads,
through SQLite's backup, so it can run any time. The integer match key the task mentions isn't done: Stage 0 measured
match_id text at about 15% of the file, mostly in the autoindex copies WITHOUT ROWID removes (docs/shared-data-stage0.md).
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, text

from draftassist.db.session import (
    drop_indexes,
    existing_dropped_indexes,
    get_engine,
    init_db,
    needs_rebuild,
    rebuild_table,
)


@dataclass
class Changes:
    rebuild: list[str] = field(default_factory=list)
    drop_indexes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.rebuild or self.drop_indexes)


def planned_changes(engine: Engine) -> Changes:
    return Changes(needs_rebuild(engine), existing_dropped_indexes(engine))


def apply_changes(engine: Engine, changes: Changes, log: Callable[[str], None] = print) -> None:
    for name in changes.rebuild:
        rebuild_table(engine, name)
        log(f"  rebuilt {name} WITHOUT ROWID")
    if changes.drop_indexes:
        drop_indexes(engine, changes.drop_indexes)
        log(f"  dropped {', '.join(changes.drop_indexes)}")
    engine.dispose()  # VACUUM needs no other connection of ours
    conn = sqlite3.connect(engine.url.database, timeout=30, isolation_level=None)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


# ---------- measuring ----------
def object_sizes(path: Path) -> dict[str, int] | None:
    """Bytes per table and index (dbstat); None if this SQLite lacks dbstat."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return dict(conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"))
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def file_bytes(path: Path) -> int:
    return sum(Path(f"{path}{s}").stat().st_size for s in ("", "-wal") if Path(f"{path}{s}").exists())


def _mb(n: float) -> str:
    return f"{n / 1e6:,.1f} MB"


# The hot queries whose plans decide which indexes stay: Strategy's builds and build order, the patch lookups, the crawl's
# per-bracket counts, History's lookups. Run with EXPLAIN QUERY PLAN by index_usage.
def hot_queries() -> list[tuple[str, str, dict]]:
    from draftassist.data import builds

    w = "(:p0, :w0)"
    params = {"champ": 1, "role": "TOP", "p0": "16.21", "w0": 1.0, "opp": 11}
    return [
        ("strategy builds", builds.COUNTS_SQL.format(values=w, table="cnt_build"), params),
        ("strategy build order", builds.COUNTS_SQL.format(values=w, table="cnt_order"), params),
        ("strategy lane hints", builds.LANE_SQL.format(values=w), params),
        ("crawled patches", "SELECT DISTINCT patch FROM crawled_matches", {}),
        ("matches per bracket", "SELECT COUNT(*) FROM crawled_matches WHERE bracket = :b", {"b": "gold"}),
        ("seeds per bracket", "SELECT puuid FROM seed_players WHERE bracket = :b", {"b": "gold"}),
        ("champion by key", "SELECT champ_id FROM champions WHERE key = :k", {"k": "Ahri"}),
        ("champion patches", "SELECT DISTINCT patch FROM champions", {}),  # data.crawler.current_patch
        ("draft by game", "SELECT draft_id FROM drafts WHERE game_id = :g", {"g": 1}),
        ("own game by match", "SELECT * FROM own_game_participants WHERE match_id = :m", {"m": "EUW1_1"}),
    ]


def query_plans(engine: Engine) -> dict[str, list[str]]:
    """Hot query name -> its EXPLAIN QUERY PLAN lines."""
    out = {}
    with engine.connect() as conn:
        for name, sql, params in hot_queries():
            try:
                out[name] = [str(r[-1]) for r in conn.execute(text("EXPLAIN QUERY PLAN " + sql), params)]
            except Exception:  # a table this database doesn't have
                continue
    return out


def index_usage(engine: Engine) -> dict[str, list[str]]:
    """Index name -> the hot queries whose plan uses it."""
    used: dict[str, list[str]] = {}
    with engine.connect() as conn:
        for name, sql, params in hot_queries():
            try:
                plan = conn.execute(text("EXPLAIN QUERY PLAN " + sql), params).fetchall()
            except Exception:  # a table this database doesn't have
                continue
            for ix in sorted({ix for row in plan for ix in re.findall(r"USING (?:COVERING )?INDEX (\w+)", str(row[-1]))}):
                used.setdefault(ix, []).append(name)
    return used


def secondary_indexes(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                                                "ORDER BY tbl_name, name"))]


# ---------- the job lock ----------
def running_job(cache_dir: Path) -> dict | None:
    """What cache/jobs/running.json says runs, if its process is alive (as the Crawl page reads it)."""
    from draftassist.jobs import pid_alive

    try:
        info = json.loads((Path(cache_dir) / "jobs" / "running.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not pid_alive(int(info.get("pid") or 0)):
        return None
    if info.get("job") == "optimize":  # this very run, started from the Crawl page as a job
        return None
    return info


# ---------- the command ----------
def _report(before: dict[str, int] | None, after: dict[str, int] | None, log) -> None:
    if before is None or after is None:
        log("  (this SQLite has no dbstat: file sizes only)")
        return
    rows = sorted(set(before) | set(after), key=lambda n: -(before.get(n, 0) - after.get(n, 0)))
    for name in rows:
        b, a = before.get(name, 0), after.get(name, 0)
        if abs(b - a) >= 50_000:
            log(f"    {name:<44} {_mb(b):>10} -> {_mb(a):>10}")


def optimize_one(path: Path, dry_run: bool, log: Callable[[str], None]) -> None:
    log(f"{path.name}:")
    if dry_run:
        with tempfile.TemporaryDirectory(prefix="draftassist-optimize-") as tmp:
            scratch = Path(tmp) / f"{path.stem}.scratch.sqlite"
            src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            dst = sqlite3.connect(scratch)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            engine = init_db(scratch)  # as the app would open it: tables a newer version added exist
            changes = planned_changes(engine)
            usage = index_usage(engine)
            unused = [ix for ix in secondary_indexes(engine) if ix not in usage]
            engine.dispose()
            sizes, size = object_sizes(scratch), file_bytes(scratch)
            log(f"  dry run on a scratch copy: rebuild {len(changes.rebuild)} tables WITHOUT ROWID"
                + (f" ({', '.join(changes.rebuild)})" if changes.rebuild else "")
                + (f"; drop {', '.join(changes.drop_indexes)}" if changes.drop_indexes else ""))
            log("  indexes the hot queries use: " + (", ".join(f"{k} ({', '.join(v)})" for k, v in sorted(usage.items()))
                                                     or "none"))
            if unused:
                log("  indexes no hot query uses (kept unless listed in db.session.DROPPED_INDEXES): " + ", ".join(unused))
            engine = get_engine(scratch)
            apply_changes(engine, changes, log=lambda *_: None)
            _report(sizes, object_sizes(scratch), log)
            log(f"  file {_mb(size)} -> {_mb(file_bytes(scratch))}")
            engine.dispose()
        return
    engine = init_db(path)
    changes = planned_changes(engine)
    if not changes:
        engine.dispose()
        log("  nothing to do")
        return
    size = file_bytes(path)
    apply_changes(engine, changes, log)
    engine.dispose()
    log(f"  file {_mb(size)} -> {_mb(file_bytes(path))}")


def optimize_databases(paths: Sequence[Path], cache_dir: Path, dry_run: bool = False,
                       log: Callable[[str], None] = print) -> int:
    if not dry_run:
        job = running_job(cache_dir)
        if job is not None:
            log(f"A {job.get('job', 'job')} job is running (pid {job.get('pid')}): stop it first, then run this again.")
            return 1
    for path in paths:
        if Path(path).is_file():
            optimize_one(Path(path), dry_run, log)
    return 0


def main() -> int:
    import argparse
    import sys

    from draftassist.config import load_settings
    from draftassist.data.master import master_path

    parser = argparse.ArgumentParser(prog=sys.argv[0], description="Smaller databases: WITHOUT ROWID, unused indexes, VACUUM")
    parser.add_argument("--dry-run", action="store_true", help="measure on a scratch copy, change nothing")
    args = parser.parse_args(sys.argv[1:])
    settings = load_settings()
    if not args.dry_run and shutil.disk_usage(settings.db_path.parent).free < 2 * file_bytes(settings.db_path):
        print("Not enough free disk space: a rebuild needs about twice the database's size.")
        return 1
    return optimize_databases([settings.db_path, master_path(settings.db_path)], settings.cache_dir, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
