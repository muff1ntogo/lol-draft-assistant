"""Users' pull (docs/shared-data.md, Stage 2): blocks newer than the local publish_id, up to the newest finished publish,
into the local databases, then the runtime tables derived from the counts (data/counts.derive), as the admin derives them.

Everything is downloaded and checked (hash per slice, parts complete) before anything is written; then each database
changes in one transaction (Master+ first, then the main one with the pull's own state), so a failure at any point leaves
the last pull in place and the next pull repeats the work. Too new a schema: refused, local data kept, "update the app".
Unreachable: the last pull stays ("Stats from <date>"). Never during champ select.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, text

from draftassist.data.shared.blocks import decode, digest
from draftassist.data.shared.schema import BY_NAME, SCHEMA_VERSION
from draftassist.data.shared.store import SharedStore, SharedStoreError

PULL_SETTING = "shared_pull"  # {"publish_id", "published_at", "pulled_at", "schema_version", "windows"}
PAGE = 20  # blocks per request: up to ~20 MB, usually far less


@dataclass
class PullStatus:
    state: str  # updated, unchanged, offline, error, too_new, too_old, draft
    publish_id: int | None = None
    blocks: int = 0
    message: str = ""


def pull_status(db: Engine) -> dict:
    with db.connect() as conn:
        row = conn.execute(text("SELECT value FROM app_settings WHERE key = :k"), {"k": PULL_SETTING}).first()
    state = json.loads(row[0]) if row else {}
    return {"publish_id": state.get("publish_id"), "published_at": state.get("published_at"),
            "pulled_at": state.get("pulled_at"), "schema_version": state.get("schema_version"),
            "windows": state.get("windows") or {}}


def _master_engine(main: Engine) -> Engine:
    from draftassist.data.master import master_path, prepare_master

    return prepare_master(main, master_path(Path(main.url.database)))


def _fetch(store: SharedStore, since: int, latest: int) -> list[tuple]:
    rows, last = [], ""
    while True:
        page = store.execute("SELECT block_id, source, tbl, slice, part, parts, schema_version, publish_id, hash, codec, "
                             "data, deleted FROM shared_blocks WHERE publish_id > ? AND publish_id <= ? AND block_id > ? "
                             "ORDER BY block_id LIMIT ?", (since, latest, last, PAGE))
        rows += page
        if len(page) < PAGE:
            return rows
        last = page[-1][0]


def _assemble(rows: list[tuple]) -> dict[tuple[str, str, str], tuple[list[str], list[list]] | None]:
    """(source, table, slice JSON) -> (columns, rows), or None for a slice that's gone. Raises ValueError on anything
    incomplete, corrupt or of another schema."""
    by_slice: dict[tuple, list[tuple]] = defaultdict(list)
    for r in rows:
        by_slice[(r[1], r[2], r[3])].append(r)
    out = {}
    for key, parts in by_slice.items():
        if key[1] not in BY_NAME:
            raise ValueError(f"unknown shared table {key[1]}")
        live = sorted((p for p in parts if not p[11]), key=lambda p: p[4])
        if not live:
            out[key] = None
            continue
        n = live[0][5]
        if [p[4] for p in live] != list(range(n)) or any(p[5] != n or p[8] != live[0][8] for p in live):
            raise ValueError(f"{key[1]} {key[2]}: parts incomplete")
        if any(p[6] != SCHEMA_VERSION or p[9] != "gzip" for p in live):
            raise ValueError(f"{key[1]}: another schema or codec")
        columns, all_rows = None, []
        for p in live:
            try:
                cols, part_rows = decode(p[10])
            except Exception as e:  # gzip, zlib or JSON: corrupt
                raise ValueError(f"{key[1]} {key[2]}: block unreadable") from e
            columns = cols
            all_rows += part_rows
        if digest(columns, all_rows) != live[0][8]:
            raise ValueError(f"{key[1]} {key[2]}: content doesn't match its hash")
        if tuple(columns) != BY_NAME[key[1]].columns:
            raise ValueError(f"{key[1]}: columns differ from this app's")
        out[key] = (columns, all_rows)
    return out


def _apply(conn, slices: dict, window: str | None, previous_window: str | None) -> None:
    from draftassist.data.counts import derive

    touched = set()
    for (_source, table, slice_json), content in sorted(slices.items()):
        t = BY_NAME[table]
        key = json.loads(slice_json)
        where = " AND ".join(f'"{c}" = :s{i}' for i, c in enumerate(t.slice_cols)) or "1 = 1"
        params = {f"s{i}": v for i, v in enumerate(key)}
        conn.execute(text(f'DELETE FROM "{table}" WHERE {where}'), params)
        if content is not None:
            columns, rows = content
            cols = ", ".join(f'"{c}"' for c in columns)
            marks = ", ".join(f":c{i}" for i in range(len(columns)))
            conn.execute(text(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})'),
                         [{f"c{i}": v for i, v in enumerate(r)} for r in rows])
        if t.kind == "counts":
            touched.add(key[0])  # the patch
    if touched or window != previous_window:
        from draftassist.data.counts import version_key

        derive(conn, sorted(touched, key=version_key), window)


def pull(main: Engine, store: SharedStore, in_draft: Callable[[], bool] = lambda: False,
         log: Callable[[str], None] = print, clock: Callable[[], float] = time.time) -> PullStatus:
    if in_draft():
        return PullStatus("draft", message="In champ select: the pull waits until the draft is over.")
    state = pull_status(main)
    try:
        latest = store.execute("SELECT publish_id, schema_version, windows, published_at FROM publish_log "
                               "ORDER BY publish_id DESC LIMIT 1")
    except SharedStoreError as e:
        if e.offline:
            return PullStatus("offline", state["publish_id"], message=str(e))
        if "no such table" in str(e):
            return PullStatus("unchanged", state["publish_id"], message="Nothing published yet.")
        raise
    if not latest:
        return PullStatus("unchanged", state["publish_id"], message="Nothing published yet.")
    publish_id, schema, windows_json, published_at = latest[0]
    if schema > SCHEMA_VERSION:
        return PullStatus("too_new", state["publish_id"],
                          message="The shared data is from a newer version: update Draft Assistant to pull it.")
    if schema < SCHEMA_VERSION:
        return PullStatus("too_old", state["publish_id"],
                          message="The shared data is from an older version: waiting for the admin to publish again.")
    since = (state["publish_id"] or 0) if state["schema_version"] == SCHEMA_VERSION else 0  # a new schema: all again
    if publish_id <= since:
        return PullStatus("unchanged", publish_id)
    try:
        rows = _fetch(store, since, publish_id)
    except SharedStoreError as e:
        if e.offline:
            return PullStatus("offline", state["publish_id"], message=str(e))
        raise
    try:
        slices = _assemble(rows)
    except ValueError as e:
        log(f"Pull refused, nothing changed: {e}")
        return PullStatus("error", state["publish_id"], message=str(e))
    windows = json.loads(windows_json or "{}")
    old_windows = state["windows"]
    by_source: dict[str, dict] = defaultdict(dict)
    for key, content in slices.items():
        by_source[key[0]][key] = content
    if by_source.get("master_plus"):
        master = _master_engine(main)
        with master.begin() as conn:
            _apply(conn, by_source["master_plus"], windows.get("master_plus"), old_windows.get("master_plus"))
    with main.begin() as conn:
        _apply(conn, by_source.get("main", {}), windows.get("main"), old_windows.get("main"))
        conn.execute(text("INSERT OR REPLACE INTO app_settings (key, value) VALUES (:k, :v)"),
                     {"k": PULL_SETTING, "v": json.dumps({"publish_id": publish_id, "published_at": published_at,
                                                         "pulled_at": clock(), "schema_version": SCHEMA_VERSION,
                                                         "windows": windows})})
    from draftassist.data.datasets import active_window, apply_selection, load_selection

    if load_selection(main).scope != "default" or active_window(main) is not None:
        apply_selection(main)  # the user's data selection, on the new counts (Stage 4)
    log(f"Pulled publish {publish_id}: {len(rows):,} blocks.")
    return PullStatus("updated", publish_id, len(rows))


def main() -> int:
    import sys

    from draftassist.config import load_settings
    from draftassist.data.shared.config import load_config, open_store
    from draftassist.db.session import init_db

    config = load_config()
    if config is None:
        print("Not signed in to a shared database: `draftassist account join <invite>` (or admin setup in Settings, Account).")
        return 1
    _ = sys.argv
    status = pull(init_db(load_settings().db_path), open_store(config))
    print(f"{status.state}" + (f": {status.message}" if status.message else "")
          + (f" (publish {status.publish_id})" if status.publish_id else ""))
    return 0 if status.state in ("updated", "unchanged") else 1


if __name__ == "__main__":
    raise SystemExit(main())
