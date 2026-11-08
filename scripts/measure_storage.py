"""Stage 0 measurement for docs/shared-data.md. Read-only: databases are opened with mode=ro, nothing is written,
nothing is printed from inside the cache bodies or .env, and no network is used. Standard library only.

    uv run python measure_storage.py                      # %LOCALAPPDATA%\\DraftAssistant (or DRAFTASSIST_HOME)
    uv run python measure_storage.py <folder> [<folder>]  # also the old checkout subfolder, a checkout, ...
    add --quick to skip the slow per-patch cardinality queries (the self-joins on match_participants)

Paste the whole output back. It holds sizes, counts and ids of patches/tiers only: no names, PUUIDs or keys.
"""
from __future__ import annotations

import gzip
import json
import lzma
import os
import random
import sqlite3
import sys
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path

QUICK = "--quick" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]


def mb(n: float) -> str:
    return f"{n / 1e6:,.1f} MB" if n < 1e9 else f"{n / 1e9:,.2f} GB"


def default_roots() -> list[Path]:
    if ARGS:
        return [Path(a) for a in ARGS]
    home = os.environ.get("DRAFTASSIST_HOME") or (Path(os.environ.get("LOCALAPPDATA", "")) / "DraftAssistant")
    roots = [Path(home)]
    here = Path.cwd()
    for cand in (here, here / "lol-draft-assistant"):
        if (cand / "data").is_dir() and cand.resolve() not in {r.resolve() for r in roots}:
            roots.append(cand)
    return roots


# ---------------------------------------------------------------- cache
def cache_report(cache: Path) -> None:
    print(f"\n## cache: {cache}")
    if not cache.is_dir():
        print("  (none)")
        return
    by_top: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_host: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    riot_files: list[Path] = []
    t = time.time()
    for dirpath, _, files in os.walk(cache):
        rel = Path(dirpath).relative_to(cache).parts
        top = rel[0] if rel else "."
        for f in files:
            p = Path(dirpath) / f
            try:
                size = p.stat().st_size
            except OSError:
                continue
            by_top[top][0] += 1
            by_top[top][1] += size
            if top == "riot" and len(rel) >= 2:
                by_host[rel[1]][0] += 1
                by_host[rel[1]][1] += size
                if f.endswith(".json"):
                    riot_files.append(p)
    files_total = sum(v[0] for v in by_top.values())
    bytes_total = sum(v[1] for v in by_top.values())
    print(f"  total {mb(bytes_total)} in {files_total:,} files (walked in {time.time() - t:.0f} s)")
    for name, (n, b) in sorted(by_top.items(), key=lambda kv: -kv[1][1]):
        print(f"  {name:<12} {mb(b):>12} {n:>10,} files")
    print("  riot/ by host:")
    for name, (n, b) in sorted(by_host.items(), key=lambda kv: -kv[1][1]):
        print(f"    {name:<10} {mb(b):>12} {n:>10,} files, mean {b / max(n, 1) / 1e3:,.1f} KB")
    if riot_files:
        compression_sample(riot_files)


def compression_sample(files: list[Path], n: int = 400) -> None:
    rnd = random.Random(1)
    sample = rnd.sample(files, min(n, len(files)))
    bodies, kinds, keys = [], Counter(), Counter()
    for p in sample:
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kinds["unreadable"] += 1
            continue
        body = entry.get("body")
        keys[entry.get("key")] += 1  # a one-way fingerprint, printed only as a count
        kinds["match" if isinstance(body, dict) and "info" in body else "list" if isinstance(body, list)
              else "null" if body is None else "other"] += 1
        bodies.append((p.stat().st_size, json.dumps(body, separators=(",", ":")).encode()))
    print(f"  sample of {len(bodies)} riot files: kinds {dict(kinds)}, distinct key fingerprints {len(keys)}")
    raw = sum(s for s, _ in bodies)
    mini = sum(len(b) for _, b in bodies)
    print(f"    on disk {mb(raw)}, body re-serialised compactly {mb(mini)}")
    train = b"".join(b for _, b in bodies[: len(bodies) // 2])[-32_768:]  # zlib preset dictionary, max 32 KB
    test = [b for _, b in bodies[len(bodies) // 2:]]
    test_raw = sum(len(b) for b in test)
    rows = []
    for name, fn in (
        ("gzip-6", lambda b: gzip.compress(b, 6)),
        ("gzip-9", lambda b: gzip.compress(b, 9)),
        ("zlib-6 + 32 KB dict", lambda b: _zdict(b, train)),
        ("lzma-6", lambda b: lzma.compress(b)),
    ):
        t = time.perf_counter()
        out = sum(len(fn(b)) for b in test)
        rows.append((name, out, (time.perf_counter() - t) / max(len(test), 1) * 1000))
    try:
        import zstandard as zstd  # only if already installed; this script installs nothing

        d = zstd.train_dictionary(110_000, [b for _, b in bodies[: len(bodies) // 2]])
        for name, c in (("zstd-3", zstd.ZstdCompressor(level=3)), ("zstd-19", zstd.ZstdCompressor(level=19)),
                        ("zstd-3 + dict", zstd.ZstdCompressor(level=3, dict_data=d))):
            t = time.perf_counter()
            out = sum(len(c.compress(b)) for b in test)
            rows.append((name, out, (time.perf_counter() - t) / len(test) * 1000))
    except Exception:
        rows.append(("zstd", 0, 0.0))
    for name, out, ms in rows:
        if out:
            print(f"    {name:<20} {test_raw / out:5.1f}x  ({ms:.2f} ms/file)")
        else:
            print(f"    {name:<20} not installed (skipped)")


def _zdict(b: bytes, zdict: bytes) -> bytes:
    c = zlib.compressobj(6, zlib.DEFLATED, 15, 9, zlib.Z_DEFAULT_STRATEGY, zdict)
    return c.compress(b) + c.flush()


# ---------------------------------------------------------------- databases
def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)


def db_report(path: Path) -> None:
    size = path.stat().st_size
    wal = path.with_name(path.name + "-wal")
    print(f"\n## {path.name}: {mb(size)}" + (f" (+ WAL {mb(wal.stat().st_size)})" if wal.exists() else ""))
    conn = open_ro(path)
    try:
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        print(f"  page size {page}, free pages {free:,} ({mb(free * page)} reclaimable by VACUUM)")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                             "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        try:
            rows = conn.execute("SELECT name, SUM(pgsize), COUNT(*) FROM dbstat GROUP BY name "
                                "ORDER BY 2 DESC").fetchall()
            print("  dbstat (tables and indexes, top 30):")
            kinds = dict(conn.execute("SELECT name, type FROM sqlite_master"))
            for name, b, _ in rows[:30]:
                rc = counts.get(name)
                extra = f"{rc:>12,} rows, {b / rc:,.0f} B/row" if rc else ""
                print(f"    {kinds.get(name, '?'):<6} {name:<46} {mb(b):>11}  {extra}")
        except sqlite3.OperationalError:
            print("  dbstat not available in this Python's SQLite: estimating from column lengths")
            for t in sorted(tables, key=lambda t: -counts[t])[:20]:
                cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
                total = conn.execute("SELECT " + " + ".join(
                    f'COALESCE(SUM(LENGTH("{c}")), 0)' for c in cols) + f' FROM "{t}"').fetchone()[0] or 0
                print(f"    {t:<46} {counts[t]:>12,} rows  ~{mb(total)} of values")
        match_id_share(conn, tables, counts)
        indexes(conn)
        if "crawled_matches" in tables and counts["crawled_matches"]:
            crawl_shape(conn, tables)
    finally:
        conn.close()


def match_id_share(conn, tables, counts) -> None:
    print("  match_id text in each table (bytes of the value alone, before page and index overhead):")
    for t in tables:
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
        if "match_id" not in cols or not counts[t]:
            continue
        b = conn.execute(f'SELECT SUM(LENGTH(match_id)) FROM "{t}"').fetchone()[0] or 0
        pk = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")') if c[5]]
        print(f"    {t:<28} {mb(b):>10}  ({counts[t]:,} rows; pk {pk})")


def indexes(conn) -> None:
    rows = conn.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' ORDER BY tbl_name").fetchall()
    print(f"  indexes ({len(rows)}): " + ", ".join(f"{t}.{n}" for n, t, _ in rows))
    print("  WITHOUT ROWID tables: " + (", ".join(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%WITHOUT ROWID%'")) or "none"))


def crawl_shape(conn, tables) -> None:
    print("  crawled matches by patch x bracket x queue:")
    rows = conn.execute("SELECT patch, bracket, queue_id, COUNT(*) FROM crawled_matches GROUP BY 1, 2, 3").fetchall()
    by_patch: dict[str, dict] = defaultdict(dict)
    for patch, br, q, n in rows:
        by_patch[patch][(br, q)] = n
    for patch in sorted(by_patch, key=lambda p: tuple(int(x) for x in p.split(".") if x.isdigit())):
        cells = by_patch[patch]
        print(f"    {patch}: {sum(cells.values()):>8,}  " + "  ".join(f"{b}/{q}:{n:,}" for (b, q), n in sorted(cells.items())))
    if "match_participants" not in tables:
        return
    latest = max(by_patch, key=lambda p: tuple(int(x) for x in p.split(".") if x.isdigit()))
    print(f"  distinct keys per (bracket, queue) on patch {latest} (sizes of the additive tables to share):")
    if QUICK:
        print("    skipped (--quick)")
        return
    t0 = time.time()
    q = {"p": latest}
    m = "SELECT match_id, bracket, queue_id FROM crawled_matches WHERE patch = :p"
    def one(sql):
        return dict(((b, qq), n) for b, qq, n in conn.execute(sql, q))
    role = one(f"WITH m AS ({m}) SELECT m.bracket, m.queue_id, COUNT(DISTINCT mp.champ_id || mp.role) "
               "FROM match_participants mp JOIN m USING (match_id) GROUP BY 1, 2")
    lanes = one(f"WITH m AS ({m}), k AS (SELECT DISTINCT m.bracket, m.queue_id, a.role, a.champ_id ca, b.champ_id cb "
                "FROM match_participants a JOIN m USING (match_id) JOIN match_participants b ON b.match_id = a.match_id "
                "AND b.role = a.role AND b.team_id <> a.team_id AND a.champ_id < b.champ_id) "
                "SELECT bracket, queue_id, COUNT(*) FROM k GROUP BY 1, 2")
    pairs = one(f"WITH m AS ({m}), k AS (SELECT DISTINCT m.bracket, m.queue_id, a.role ra, a.champ_id ca, b.role rb, "
                "b.champ_id cb FROM match_participants a JOIN m USING (match_id) JOIN match_participants b "
                "ON b.match_id = a.match_id AND b.team_id = a.team_id "
                "AND (a.role < b.role OR (a.role = b.role AND a.champ_id < b.champ_id))) "
                "SELECT bracket, queue_id, COUNT(*) FROM k GROUP BY 1, 2")
    builds = {}
    if "match_builds" in tables:
        builds = one(f"WITH m AS ({m}), k AS (SELECT DISTINCT m.bracket, m.queue_id, b.champ_id, p.role, "
                     "b.perks_json, b.spells_json FROM match_builds b JOIN m USING (match_id) JOIN match_participants p "
                     "ON p.match_id = b.match_id AND p.champ_id = b.champ_id) SELECT bracket, queue_id, COUNT(*) FROM k "
                     "GROUP BY 1, 2")
        items = one(f"WITH m AS ({m}), k AS (SELECT DISTINCT m.bracket, m.queue_id, b.champ_id, p.role, j.value "
                    "FROM match_builds b JOIN m USING (match_id) JOIN match_participants p ON p.match_id = b.match_id "
                    "AND p.champ_id = b.champ_id, json_each(b.items_json) j) SELECT bracket, queue_id, COUNT(*) FROM k "
                    "GROUP BY 1, 2")
        opp = one(f"WITH m AS ({m}), k AS (SELECT DISTINCT m.bracket, m.queue_id, p.champ_id, p.role, o.champ_id oc, "
                  "b.keystone FROM match_builds b JOIN m USING (match_id) JOIN match_participants p "
                  "ON p.match_id = b.match_id AND p.champ_id = b.champ_id JOIN match_participants o "
                  "ON o.match_id = p.match_id AND o.role = p.role AND o.team_id <> p.team_id) "
                  "SELECT bracket, queue_id, COUNT(*) FROM k GROUP BY 1, 2")
    print(f"    {'bracket/queue':<16}{'role':>8}{'lanes':>9}{'pairs':>10}{'pages+spells':>14}{'items':>9}{'opp x keystone':>16}")
    tot = Counter()
    for key in sorted(set(role) | set(lanes) | set(pairs)):
        vals = [role.get(key, 0), lanes.get(key, 0), pairs.get(key, 0), builds.get(key, 0),
                items.get(key, 0) if builds else 0, opp.get(key, 0) if builds else 0]
        for i, v in enumerate(vals):
            tot[i] += v
        print(f"    {key[0] + '/' + str(key[1]):<16}" + "".join(f"{v:>{w},}" for v, w in zip(vals, (8, 9, 10, 14, 9, 16))))
    print(f"    {'total':<16}" + "".join(f"{tot[i]:>{w},}" for i, w in enumerate((8, 9, 10, 14, 9, 16))))
    print(f"    ({time.time() - t0:.0f} s)")


def main() -> int:
    print(f"measure_storage.py, Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    for root in default_roots():
        print(f"\n# {root}")
        if not root.is_dir():
            print("  (missing)")
            continue
        cache_report(root / "cache")
        dbs = sorted((root / "data").glob("*.sqlite")) if (root / "data").is_dir() else []
        if not dbs:
            print("\n  no data/*.sqlite")
        for p in dbs:
            db_report(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
