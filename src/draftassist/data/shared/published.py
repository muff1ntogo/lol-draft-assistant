"""`draftassist shared-inspect`: what is published, decoded (decision 2: Turso's console only shows the blocks). Read-only.

    draftassist shared-inspect                       the publishes, and blocks per source and table
    draftassist shared-inspect --table cnt_role --patch 16.21 [--rows 20]
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone

from draftassist.data.shared.blocks import decode


def inspect_store(store, table: str | None = None, patch: str | None = None, rows: int = 10,
                  log: Callable[[str], None] = print) -> dict:
    logs = store.execute("SELECT publish_id, published_at, app_version, schema_version, rows_written, windows "
                         "FROM publish_log ORDER BY publish_id DESC LIMIT 10")
    for pid, at, version, schema, written, windows in logs:
        when = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log(f"publish {pid}: {when}, app {version}, schema {schema}, {written:,} rows written, windows {windows}")
    blocks = store.execute("SELECT source, tbl, slice, part, parts, deleted, LENGTH(data) FROM shared_blocks")
    summary: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
    for source, tbl, _slice, _part, _parts, deleted, size in blocks:
        s = summary[(source, tbl)]
        s[0] += 0 if deleted else 1
        s[1] += 1 if deleted else 0
        s[2] += size or 0
    log(f"{'source':<12}{'table':<20}{'blocks':>8}{'removed':>9}{'size':>12}")
    for (source, tbl), (n, gone, size) in sorted(summary.items()):
        log(f"{source:<12}{tbl:<20}{n:>8,}{gone:>9,}{size / 1e6:>10.2f} MB")
    if table:
        found = store.execute("SELECT slice, part, data FROM shared_blocks WHERE tbl = ? AND deleted = 0 "
                              "ORDER BY slice, part", (table,))
        shown = 0
        for slice_json, part, data in found:
            key = json.loads(slice_json)
            if patch and (not key or key[0] != patch):
                continue
            columns, block_rows = decode(data)
            log(f"{table} {key} part {part}: {len(block_rows):,} rows")
            log("  " + " | ".join(columns))
            for r in block_rows[:max(rows - shown, 0)]:
                log("  " + " | ".join(str(v) for v in r))
            shown += min(len(block_rows), rows)
    return {"publishes": len(logs), "tables": {f"{s}|{t}": v for (s, t), v in summary.items()}}


def main() -> int:
    import argparse
    import sys

    from draftassist.data.shared.config import load_config, open_store

    parser = argparse.ArgumentParser(prog=sys.argv[0], description="Show what is published in the shared database")
    parser.add_argument("--table", help="decode this table's blocks")
    parser.add_argument("--patch", help="only slices of this patch")
    parser.add_argument("--rows", type=int, default=10, help="rows to show")
    args = parser.parse_args(sys.argv[1:])
    config = load_config()
    if config is None:
        print("Not signed in to a shared database: `draftassist account join <invite>` (or admin setup in Settings, Account).")
        return 1
    inspect_store(open_store(config), args.table, args.patch, args.rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
