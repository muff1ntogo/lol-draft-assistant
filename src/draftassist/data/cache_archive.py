"""Move cached match bodies of old patches out of cache.sqlite into one `cache-archive-<patch>.sqlite` per patch. Off
unless run; backfill and RiotClient still read the archives (data/response_cache.py).

    draftassist cache-archive --older-than-patches 6

Keeps the newest N patches found in the cache's match bodies (`info.gameVersion`); id lists, leagues and the rest stay.
Each archive holds its rows exactly as stored (no re-compression) plus the dictionaries, so it reads on its own, and it is a
single file (no WAL) that can be moved or zipped. A zipped archive isn't read: unzip it back beside cache.sqlite first.
Rows are copied and committed before they are deleted from the cache, so a stop in between leaves them in both.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from draftassist.data.response_cache import ARCHIVE_PREFIX, CACHE_FILE, SCHEMA, decode, open_cache

CHUNK = 500


def patch_of(version: str | None) -> str | None:
    """'16.21.712.4501' -> '16.21'."""
    parts = (version or "").split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() else None


def _order(patch: str) -> tuple[int, int]:
    major, minor = patch.split(".")
    return int(major), int(minor)


def archive_path(cache_dir: Path, patch: str) -> Path:
    return Path(cache_dir) / f"{ARCHIVE_PREFIX}{patch}.sqlite"


def archive_cache(cache_dir: Path, older_than_patches: int = 6, log: Callable[[str], None] = print) -> dict[str, int]:
    """Returns rows moved per patch."""
    from draftassist.data.riot import REGION

    cache_dir = Path(cache_dir)
    cache = open_cache(cache_dir)
    by_patch: dict[str, list[str]] = defaultdict(list)
    for key, _host, _f, _k, codec, blob in cache.rows(REGION):
        try:
            body = json.loads(decode(codec, blob, cache.store.dictionary))
        except (ValueError, LookupError):
            continue
        if isinstance(body, dict) and isinstance(body.get("info"), dict):
            patch = patch_of(body["info"].get("gameVersion"))
            if patch:
                by_patch[patch].append(key)
    keep = sorted(by_patch, key=_order, reverse=True)[:max(older_than_patches, 1)]
    moved: dict[str, int] = {}
    with cache._lock:
        main = cache.store.conn(create=False)
        if main is None:
            return moved
        dicts = main.execute("SELECT dict_id, created_at, data FROM dictionaries").fetchall()
        for patch in sorted(set(by_patch) - set(keep), key=_order):
            keys = by_patch[patch]
            arch = sqlite3.connect(archive_path(cache_dir, patch), isolation_level=None)
            try:
                arch.execute("PRAGMA journal_mode=DELETE")  # one file
                arch.executescript(SCHEMA)
                arch.execute("BEGIN")
                arch.executemany("INSERT OR REPLACE INTO dictionaries VALUES (?, ?, ?)", dicts)
                for i in range(0, len(keys), CHUNK):
                    chunk = keys[i:i + CHUNK]
                    rows = main.execute("SELECT key, host, fetched_at, key_id, codec, body FROM responses WHERE key IN "
                                        f"({','.join('?' * len(chunk))})", chunk).fetchall()
                    arch.executemany("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?, ?)", rows)
                arch.execute("COMMIT")
                copied = sum(arch.execute(f"SELECT COUNT(*) FROM responses WHERE key IN ({','.join('?' * len(c))})",
                                          c).fetchone()[0] for c in (keys[i:i + CHUNK] for i in range(0, len(keys), CHUNK)))
            finally:
                arch.close()
            if copied != len(keys):
                raise RuntimeError(f"archive for {patch} holds {copied} of {len(keys)} rows: nothing deleted")
            main.execute("BEGIN IMMEDIATE")
            try:
                for i in range(0, len(keys), CHUNK):
                    chunk = keys[i:i + CHUNK]
                    main.execute(f"DELETE FROM responses WHERE key IN ({','.join('?' * len(chunk))})", chunk)
                main.execute("COMMIT")
            except BaseException:
                main.execute("ROLLBACK")
                raise
            moved[patch] = len(keys)
            log(f"  {patch}: {len(keys):,} match bodies -> {archive_path(cache_dir, patch).name}")
        if moved:
            before = (cache_dir / CACHE_FILE).stat().st_size
            main.execute("VACUUM")
            log(f"cache.sqlite {before / 1e6:,.1f} MB -> {(cache_dir / CACHE_FILE).stat().st_size / 1e6:,.1f} MB")
    if not moved:
        log(f"Nothing older than the newest {older_than_patches} patches ({', '.join(keep) or 'none cached'})")
    return moved


def main() -> int:
    import argparse
    import sys

    from draftassist.config import load_settings

    parser = argparse.ArgumentParser(prog=sys.argv[0], description="Move old patches' match bodies into archive files")
    parser.add_argument("--older-than-patches", type=int, default=6, help="keep this many newest patches (default 6)")
    args = parser.parse_args(sys.argv[1:])
    archive_cache(load_settings().cache_dir, args.older_than_patches)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
