"""Move the older one-.json-file-per-request Riot cache into cache.sqlite (data/response_cache.py), then delete the files.

    draftassist cache-pack [--keep] [--batch 2000]

Files go in batches of 2,000, one transaction each. After a batch commits, every row is read back and decoded, and only
a file whose row gives the same canonical JSON, fetch time and key fingerprint is deleted. So it is resumable: stopped
before a commit, the batch's files are still there; stopped after it, the next run finds the rows and deletes the files.
A file whose row is newer (fetched again since) is simply deleted. Unreadable files are left where they are and counted.
The first run trains the zstd dictionary from a sample of the files. Safe while a crawl runs: the crawl's new responses
are rows already, and a file it reads just as it is deleted is a cache miss, fetched again.
"""
from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from draftassist.data.response_cache import (
    CACHE_FILE,
    LEGACY_DIR,
    ResponseCache,
    canonical,
    decode,
    open_cache,
)

BATCH = 2_000
TRAIN_SAMPLE = 2_000


class PackError(RuntimeError):
    """A packed row didn't read back as its file: nothing of that batch was deleted."""


@dataclass
class PackResult:
    packed: int = 0  # rows written
    already: int = 0  # a row with the same or a newer fetch was there
    deleted: int = 0
    unreadable: int = 0
    before_bytes: int = 0
    after_bytes: int = 0
    dictionary: int | None = None  # the dictionary new rows use


def _mb(n: int) -> str:
    return f"{n / 1e6:,.1f} MB" if n < 1e9 else f"{n / 1e9:,.2f} GB"


def legacy_files(cache_dir: Path) -> list[Path]:
    root = Path(cache_dir) / LEGACY_DIR
    return sorted(root.glob("*/*.json")) if root.is_dir() else []


def _size(paths) -> int:
    total = 0
    for p in paths:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def cache_bytes(cache_dir: Path) -> int:
    """The legacy files plus cache.sqlite and its WAL."""
    db = Path(cache_dir) / CACHE_FILE
    return _size(legacy_files(cache_dir)) + _size([db, Path(f"{db}-wal")])


def _read(path: Path) -> dict | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return entry if isinstance(entry, dict) and "body" in entry else None


def _train(cache: ResponseCache, files: list[Path]) -> int | None:
    sample = random.Random(1).sample(files, min(TRAIN_SAMPLE, len(files)))
    bodies = [canonical(e["body"]) for e in map(_read, sample) if e is not None]
    return cache.train_dictionary(bodies)


def pack_cache(cache_dir: Path, keep: bool = False, batch_size: int = BATCH, limit: int | None = None,
               on_event: Callable[[str, int], None] | None = None, log: Callable[[str], None] = print) -> PackResult:
    """`on_event(stage, batch)` is called with "inserting" halfway through a batch's inserts and "committed" after its
    commit (tests use it to stop there). `limit`: at most this many files this run."""
    cache_dir = Path(cache_dir)
    cache = open_cache(cache_dir)
    result = PackResult(before_bytes=cache_bytes(cache_dir))
    files = legacy_files(cache_dir)
    if limit is not None:
        files = files[:limit]
    newest = cache.store.newest_dictionary()
    result.dictionary = newest[0] if newest else _train(cache, files)
    log(f"{len(files):,} cache files to pack" + (f", dictionary {result.dictionary}" if result.dictionary else ""))

    for b, start in enumerate(range(0, len(files), batch_size)):
        batch = files[start:start + batch_size]
        entries: list[tuple[Path, str, str, dict]] = []
        for path in batch:
            entry = _read(path)
            if entry is None:
                result.unreadable += 1
                continue
            entries.append((path, path.parent.name, path.stem, entry))
        written: set[str] = set()
        with cache._lock:
            conn = cache.store.conn(create=True)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for i, (path, host, key, entry) in enumerate(entries):
                    if on_event is not None and i == len(entries) // 2:
                        on_event("inserting", b)
                    row = conn.execute("SELECT fetched_at FROM responses WHERE key = ?", (key,)).fetchone()
                    fetched_at = entry.get("fetched_at")
                    if row is not None and (row[0] or 0) >= (fetched_at or 0):
                        continue
                    codec, blob = cache.encode_body(entry["body"])
                    conn.execute("INSERT OR REPLACE INTO responses (key, host, fetched_at, key_id, codec, body) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (key, host, fetched_at, entry.get("key"), codec, blob))
                    written.add(key)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        if on_event is not None:
            on_event("committed", b)
        # verify every row against its file before deleting anything of this batch
        for path, host, key, entry in entries:
            row = cache.store.row(key)
            if row is None:
                raise PackError(f"{path.name}: no row after commit")
            fetched_at, key_id, codec, blob = row
            if key in written:
                same = (decode(codec, blob, cache.store.dictionary) == canonical(entry["body"])
                        and fetched_at == entry.get("fetched_at") and key_id == entry.get("key"))
                if not same:
                    raise PackError(f"{path.name}: the packed row doesn't match the file; nothing of this batch deleted")
            else:
                decode(codec, blob, cache.store.dictionary)  # a newer row: it must at least decode
        result.packed += len(written)
        result.already += len(entries) - len(written)
        if not keep:
            for path, *_ in entries:
                path.unlink(missing_ok=True)
                result.deleted += 1
        log(f"  batch {b + 1}: {start + len(batch):,} / {len(files):,} files")

    with cache._lock:
        conn = cache.store.conn(create=False)
        if conn is not None:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if not keep:
        root = cache_dir / LEGACY_DIR
        for d in sorted(root.glob("*"), reverse=True) if root.is_dir() else []:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    result.after_bytes = cache_bytes(cache_dir)
    log(f"Packed {result.packed:,}, already packed {result.already:,}, deleted {result.deleted:,} files, "
        f"unreadable {result.unreadable:,}. Cache before {_mb(result.before_bytes)}, after {_mb(result.after_bytes)}")
    return result


def main() -> int:
    import argparse
    import sys

    from draftassist.config import load_settings

    parser = argparse.ArgumentParser(prog=sys.argv[0], description="Pack the Riot cache's .json files into cache.sqlite")
    parser.add_argument("--keep", action="store_true", help="keep the .json files after packing them")
    parser.add_argument("--batch", type=int, default=BATCH, help="files per transaction")
    args = parser.parse_args(sys.argv[1:])
    settings = load_settings()
    try:
        pack_cache(settings.cache_dir, keep=args.keep, batch_size=args.batch)
    except PackError as e:
        print(f"Stopped: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
