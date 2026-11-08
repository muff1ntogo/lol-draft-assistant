"""The admin's publish (docs/shared-data.md, Stage 2): the shared tables of the main and Master+ databases, as blocks,
written to the shared store only where they changed.

- What is published is recorded locally (`publish_state`: one row per slice, its hash and parts), so an unchanged slice
  writes nothing, and a second publish without changes writes 0 rows.
- Every block of one publish carries its `publish_id`; the `publish_log` row is written last, so pulls (which read only
  up to the newest logged publish) never see half a publish. Stopped halfway, the next run resumes the same publish_id
  and writes what's left.
- A slice that's gone is tombstoned (deleted = 1, same publish_id), as are parts a slice no longer has.
- The quota meter: rows written this month (counted here) against the plan's monthly rows; a warning from 70%, refused
  past 95% (decision 2: blocks keep a month around 10k rows of the free tier's 10 M).
- Never during champ select.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import Engine, text

from draftassist.data.shared.blocks import MAX_BLOCK_BYTES, Slice, digest, encode_parts, table_slices
from draftassist.data.shared.schema import REMOTE_SCHEMA, SCHEMA_VERSION
from draftassist.data.shared.store import SharedStore

QUOTA_SETTING = "shared_quota"  # {"month": "2026-11", "rows": 1234}
IN_PROGRESS_SETTING = "shared_publish"  # {"publish_id": 7} while a publish isn't finished
BATCH_BYTES = 3_000_000
FREE_TIER_ROWS = 10_000_000  # Turso free tier, rows written per month (turso.tech/pricing, 8 Nov 2026)

_INSERT = ("INSERT OR REPLACE INTO shared_blocks (block_id, source, tbl, slice, part, parts, schema_version, publish_id, "
           "hash, codec, data, deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


@dataclass(frozen=True)
class Quota:
    monthly_rows: int = FREE_TIER_ROWS
    warn: float = 0.70
    stop: float = 0.95


@dataclass
class PublishResult:
    state: str  # published, unchanged, quota, draft, dry_run
    publish_id: int | None = None
    blocks_written: int = 0  # block rows with data
    tombstones: int = 0
    rows_written: int = 0  # every row written, the log's included (what the quota counts)
    blocks_planned: int = 0
    messages: list[str] = field(default_factory=list)


def _setting(conn, key: str):
    row = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).first()
    return json.loads(row[0]) if row else None


def _save(conn, key: str, value) -> None:
    conn.execute(text("INSERT OR REPLACE INTO app_settings (key, value) VALUES (:k, :v)"),
                 {"k": key, "v": json.dumps(value)})


def _month(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m")


def rows_this_month(db: Engine, clock: Callable[[], float] = time.time) -> int:
    with db.connect() as conn:
        q = _setting(conn, QUOTA_SETTING) or {}
    return int(q.get("rows", 0)) if q.get("month") == _month(clock) else 0


def _count_rows(db: Engine, n: int, clock) -> None:
    with db.begin() as conn:
        q = _setting(conn, QUOTA_SETTING) or {}
        month = _month(clock)
        rows = int(q.get("rows", 0)) if q.get("month") == month else 0
        _save(conn, QUOTA_SETTING, {"month": month, "rows": rows + n})


@dataclass
class _Plan:
    slice: Slice | None
    slice_key: str
    hash: str
    parts: list[bytes]
    old_parts: int  # parts published before (tombstoned beyond the new count)

    def statements(self, publish_id: int) -> list[tuple[str, tuple]]:
        source, table, slice_json = self.slice_key.split("|", 2)
        out = []
        for i, data in enumerate(self.parts):
            out.append((_INSERT, (f"{self.slice_key}|{i}", source, table, slice_json, i, len(self.parts), SCHEMA_VERSION,
                                  publish_id, self.hash, "gzip", data, 0)))
        for i in range(len(self.parts), self.old_parts):
            out.append((_INSERT, (f"{self.slice_key}|{i}", source, table, slice_json, i, len(self.parts), SCHEMA_VERSION,
                                  publish_id, self.hash, "gzip", None, 1)))
        return out


def plan_publish(sources: dict[str, Engine | None], state: dict[str, tuple[str, int]],
                 max_block_bytes: int) -> list[_Plan]:
    plans = []
    seen = set()
    for source, engine in sources.items():
        for s in table_slices(engine, source):
            seen.add(s.slice_key)
            h = digest(s.columns, s.rows)
            old = state.get(s.slice_key)
            parts = encode_parts(s.columns, s.rows, max_block_bytes)
            if old is not None and old[0] == h and old[1] == len(parts):
                continue
            plans.append(_Plan(s, s.slice_key, h, parts, old[1] if old else 0))
    for key, (h, parts) in sorted(state.items()):
        if key not in seen and parts > 0:  # gone: every part tombstoned
            plans.append(_Plan(None, key, "", [], parts))
    return plans


def publish(main: Engine, master: Engine | None, store: SharedStore, app_version: str,
            in_draft: Callable[[], bool] = lambda: False, log: Callable[[str], None] = print,
            quota: Quota | None = None, batch_bytes: int = BATCH_BYTES, max_block_bytes: int = MAX_BLOCK_BYTES,
            dry_run: bool = False, clock: Callable[[], float] = time.time) -> PublishResult:
    from draftassist.data.synergy import latest_window

    quota = quota or Quota()
    if in_draft():
        log("In champ select: publishing waits until the draft is over.")
        return PublishResult("draft")
    with main.connect() as conn:
        state = {k: (h, p) for k, h, p in conn.execute(text("SELECT slice_key, hash, parts FROM publish_state"))}
        in_progress = _setting(conn, IN_PROGRESS_SETTING)
    plans = plan_publish({"main": main, "master_plus": master}, state, max_block_bytes)
    statements = sum(len(p.parts) + max(p.old_parts - len(p.parts), 0) for p in plans)
    result = PublishResult("unchanged", blocks_planned=statements)
    if not plans and in_progress is None:
        log("Nothing changed since the last publish: 0 rows written.")
        return result
    if dry_run:
        result.state = "dry_run"
        log(f"Would write {statements:,} blocks.")
        return result

    used, planned = rows_this_month(main, clock), statements + 1
    share = (used + planned) / quota.monthly_rows
    meter = (f"Rows written this month: {used:,} + {planned:,} now = {used + planned:,} of {quota.monthly_rows:,} "
             f"({share:.0%})")
    if share > quota.stop:
        log(f"{meter}. Over {quota.stop:.0%} of the monthly quota: not published.")
        result.state = "quota"
        return result
    log(meter + (f". Warning: past {quota.warn:.0%} of the monthly quota." if share >= quota.warn else "."))

    for sql in REMOTE_SCHEMA:
        store.execute(sql)
    if in_progress is not None:
        publish_id = int(in_progress["publish_id"])
    else:
        top = store.execute("SELECT MAX(m) FROM (SELECT MAX(publish_id) AS m FROM publish_log "
                            "UNION ALL SELECT MAX(publish_id) FROM shared_blocks)")
        publish_id = int((top[0][0] if top and top[0][0] is not None else 0)) + 1
        with main.begin() as conn:
            _save(conn, IN_PROGRESS_SETTING, {"publish_id": publish_id})
    result.publish_id = publish_id

    batch: list[_Plan] = []
    size = 0

    def flush() -> None:
        nonlocal batch, size
        if not batch:
            return
        stmts = [st for p in batch for st in p.statements(publish_id)]
        store.batch(stmts)
        with main.begin() as conn:
            for p in batch:
                if p.slice is None:
                    conn.execute(text("DELETE FROM publish_state WHERE slice_key = :k"), {"k": p.slice_key})
                else:
                    conn.execute(text("INSERT OR REPLACE INTO publish_state (slice_key, hash, parts, publish_id) "
                                      "VALUES (:k, :h, :p, :id)"),
                                 {"k": p.slice_key, "h": p.hash, "p": len(p.parts), "id": publish_id})
        _count_rows(main, len(stmts), clock)
        result.rows_written += len(stmts)
        result.blocks_written += sum(len(p.parts) for p in batch)
        result.tombstones += sum(max(p.old_parts - len(p.parts), 0) for p in batch)
        batch, size = [], 0

    for p in plans:  # a slice's parts always go in one batch, so a slice is never half written
        batch.append(p)
        size += sum(len(d) for d in p.parts)
        if size >= batch_bytes:
            flush()
    flush()

    windows = {"main": latest_window(main), "master_plus": latest_window(master) if master is not None else None}
    store.batch([("INSERT OR REPLACE INTO publish_log (publish_id, published_at, app_version, schema_version, "
                  "dataset_ids, rows_written, windows) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (publish_id, clock(), app_version, SCHEMA_VERSION, json.dumps(["default"]),
                   result.rows_written + 1, json.dumps(windows)))])
    _count_rows(main, 1, clock)
    result.rows_written += 1
    with main.begin() as conn:
        conn.execute(text("DELETE FROM app_settings WHERE key = :k"), {"k": IN_PROGRESS_SETTING})
    result.state = "published"
    log(f"Published {publish_id}: {result.blocks_written:,} blocks, {result.tombstones:,} removed, "
        f"{result.rows_written:,} rows written.")
    return result


def ensure_counts(db: Engine, log: Callable[[str], None] = print) -> list[str]:
    """Aggregate crawled patches that have no counts yet (a database from before Stage 2), so nothing is left out."""
    from draftassist.data.aggregate import aggregate
    from draftassist.data.crawler import current_patch

    with db.connect() as conn:
        missing = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT patch FROM crawled_matches WHERE patch NOT IN (SELECT patch FROM cnt_bracket)"))]
    if not missing:
        return []
    try:
        window = current_patch(db)
    except RuntimeError:
        window = None
    for patch in missing:
        log(f"Counting patch {patch} (first publish since the count tables)...")
        aggregate(db, patch, window=window)
    return missing


def app_version() -> str:
    try:
        from importlib.metadata import version

        return version("draftassist")
    except Exception:
        return "dev"


def main() -> int:
    import argparse
    import sys

    from draftassist.config import load_settings
    from draftassist.data.master import master_path
    from draftassist.data.progress import yield_requested
    from draftassist.data.shared.config import load_config, open_store
    from draftassist.data.shared.store import SharedStoreError
    from draftassist.db.session import init_db

    parser = argparse.ArgumentParser(prog=sys.argv[0], description="Publish the crawl's counts and fits (admin)")
    parser.add_argument("--dry-run", action="store_true", help="say what would be written, write nothing")
    args = parser.parse_args(sys.argv[1:])
    config = load_config()
    if config is None:
        print("Not signed in to a shared database: `draftassist account join <invite>` (or admin setup in Settings, Account).")
        return 1
    settings = load_settings()
    main_db = init_db(settings.db_path)
    master = init_db(master_path(settings.db_path)) if master_path(settings.db_path).is_file() else None
    for db in (main_db, master):
        if db is not None:
            ensure_counts(db)
    from draftassist.data.datasets import ensure_datasets, fit_datasets

    ensure_datasets(main_db)
    if not args.dry_run and fit_datasets(main_db):  # each dataset's own fits (Stage 4), when the counts changed
        print("Fitted the datasets on the new counts.")
    yield_file = settings.cache_dir / "jobs" / "yield.json"
    try:
        result = publish(main_db, master, open_store(config), app_version(), dry_run=args.dry_run,
                         in_draft=lambda: yield_requested(yield_file))
    except SharedStoreError as e:
        print(f"Stopped: {e}. What was written is kept; the next publish carries on.")
        return 1
    return 0 if result.state in ("published", "unchanged", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
