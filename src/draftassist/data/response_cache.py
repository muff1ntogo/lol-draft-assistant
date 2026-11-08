"""The Riot response cache in one SQLite file, `<cache>/cache.sqlite` (docs/shared-data.md, Stage 1).

One row per request: `key` is the request's fingerprint (the same sha1 the old `<cache>/riot/<host>/<key>.json` files were
named by), `key_id` the API key's fingerprint (riot.key_id: an entry from another key is a miss for RiotClient), `codec`
how `body` is compressed. The codec is per row, so rows written by different versions read side by side:
- `zstd-d<N>`: zstd level 3 with trained dictionary N (the `dictionaries` table; never changed once rows use it).
  Measured on the real cache: 10.3x against gzip-6's 6.8x (docs/shared-data-stage0.md, decision 3).
- `zstd` (before a dictionary is trained), `gzip`, `json` (uncompressed).

Reads try a row, then the legacy `.json` file (a half-packed cache works; data.cache_pack moves them), then the archives
`cache-archive-<patch>.sqlite` (data.cache_archive). Writes always go to the row. The body is stored as canonical JSON, so
a body decodes to the same bytes whichever codec stored it.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

CACHE_FILE = "cache.sqlite"
ARCHIVE_PREFIX = "cache-archive-"
LEGACY_DIR = "riot"
ZSTD_LEVEL = 3
DICT_SIZE = 112_640  # zstd's default dictionary size (110 KB)
MIN_TRAIN_SAMPLES = 100  # fewer bodies than this: no dictionary yet, rows use plain zstd

SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    fetched_at REAL,
    key_id TEXT,
    codec TEXT NOT NULL,
    body BLOB NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dictionaries (
    dict_id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    data BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class Entry:
    fetched_at: float | None
    key_id: str | None
    body: Any


def request_key(host: str, path: str, params: dict | None) -> str:
    """The request's fingerprint; also the legacy file's name (riot.cache_path)."""
    key = json.dumps([host, path, sorted((params or {}).items())])
    return hashlib.sha1(key.encode()).hexdigest()


def canonical(body: Any) -> bytes:
    """The one serialisation every codec stores: compact, UTF-8, key order kept."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------- codecs ----------
def encode(raw: bytes, codec: str = "zstd", dict_id: int | None = None,
           zdict: zstandard.ZstdCompressionDict | None = None) -> tuple[str, bytes]:
    if codec == "json":
        return "json", raw
    if codec == "gzip":
        return "gzip", gzip.compress(raw, 6)
    if codec == "zstd":
        if zdict is not None and dict_id is not None:
            return f"zstd-d{dict_id}", zstandard.ZstdCompressor(level=ZSTD_LEVEL, dict_data=zdict).compress(raw)
        return "zstd", zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(raw)
    raise ValueError(f"unknown codec {codec!r}")


def decode(codec: str, blob: bytes, dictionary: Callable[[int], zstandard.ZstdCompressionDict | None]) -> bytes:
    if codec == "json":
        return bytes(blob)
    if codec == "gzip":
        return gzip.decompress(blob)
    if codec == "zstd":
        return zstandard.ZstdDecompressor().decompress(blob)
    if codec.startswith("zstd-d"):
        zdict = dictionary(int(codec[len("zstd-d"):]))
        if zdict is None:
            raise LookupError(f"dictionary for {codec} is missing")
        return zstandard.ZstdDecompressor(dict_data=zdict).decompress(blob)
    raise ValueError(f"unknown codec {codec!r}")


# ---------- one SQLite file (the cache or an archive) ----------
class Store:
    def __init__(self, path: Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self._conn: sqlite3.Connection | None = None
        self._dicts: dict[int, zstandard.ZstdCompressionDict] = {}

    def conn(self, create: bool) -> sqlite3.Connection | None:
        if self._conn is None:
            if not self.path.is_file() and (self.readonly or not create):
                return None
            if self.readonly:
                c = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=30,
                                    check_same_thread=False, isolation_level=None)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                c = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
                c.execute("PRAGMA journal_mode=WAL")  # a crawl writes while the app and backfill read
                c.executescript(SCHEMA)
            c.execute("PRAGMA busy_timeout=30000")
            self._conn = c
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def dictionary(self, dict_id: int) -> zstandard.ZstdCompressionDict | None:
        if dict_id not in self._dicts:
            c = self.conn(create=False)
            row = c.execute("SELECT data FROM dictionaries WHERE dict_id = ?", (dict_id,)).fetchone() if c else None
            if row is None:
                return None
            self._dicts[dict_id] = zstandard.ZstdCompressionDict(bytes(row[0]))
        return self._dicts[dict_id]

    def newest_dictionary(self) -> tuple[int, zstandard.ZstdCompressionDict] | None:
        c = self.conn(create=False)
        row = c.execute("SELECT MAX(dict_id) FROM dictionaries").fetchone() if c else None
        if not row or row[0] is None:
            return None
        return row[0], self.dictionary(row[0])

    def row(self, key: str) -> tuple | None:
        c = self.conn(create=False)
        if c is None:
            return None
        return c.execute("SELECT fetched_at, key_id, codec, body FROM responses WHERE key = ?", (key,)).fetchone()

    def entry(self, key: str) -> Entry | None:
        r = self.row(key)
        if r is None:
            return None
        fetched_at, kid, codec, blob = r
        return Entry(fetched_at, kid, json.loads(decode(codec, blob, self.dictionary)))

    def has(self, key: str) -> bool:
        c = self.conn(create=False)
        return bool(c and c.execute("SELECT 1 FROM responses WHERE key = ?", (key,)).fetchone())


def _legacy_entry(path: Path) -> Entry | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or "body" not in entry:
        return None
    return Entry(entry.get("fetched_at"), entry.get("key"), entry["body"])


class ResponseCache:
    """Thread-safe: one connection under a lock (the crawl's threads share a RiotClient)."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.store = Store(self.cache_dir / CACHE_FILE)
        self._lock = threading.RLock()
        self._archives: dict[Path, Store] = {}
        self._writer: tuple[int, zstandard.ZstdCompressionDict] | None | bool = False  # False: not looked up yet

    def close(self) -> None:
        with self._lock:
            self.store.close()
            for a in self._archives.values():
                a.close()
            self._archives.clear()
            self._writer = False

    # ---------- reading ----------
    def legacy_path(self, host: str, key: str) -> Path:
        return self.cache_dir / LEGACY_DIR / host / f"{key}.json"

    def archives(self) -> list[Store]:
        paths = sorted(self.cache_dir.glob(f"{ARCHIVE_PREFIX}*.sqlite"))
        for p in paths:
            if p not in self._archives:
                self._archives[p] = Store(p, readonly=True)
        return [self._archives[p] for p in paths]

    def get_key(self, host: str, key: str) -> Entry | None:
        with self._lock:
            entry = self.store.entry(key)
            if entry is not None:
                return entry
            entry = _legacy_entry(self.legacy_path(host, key))
            if entry is not None:
                return entry
            for archive in self.archives():
                entry = archive.entry(key)
                if entry is not None:
                    return entry
        return None

    def get(self, host: str, path: str, params: dict | None) -> Entry | None:
        return self.get_key(host, request_key(host, path, params))

    def has_key(self, host: str, key: str) -> bool:
        with self._lock:
            return (self.store.has(key) or self.legacy_path(host, key).is_file()
                    or any(a.has(key) for a in self.archives()))

    # ---------- writing ----------
    def _write_dict(self) -> tuple[int, zstandard.ZstdCompressionDict] | None:
        if self._writer is False:
            self._writer = self.store.newest_dictionary()
        return self._writer  # type: ignore[return-value]

    def encode_body(self, body: Any) -> tuple[str, bytes]:
        d = self._write_dict()
        return encode(canonical(body), "zstd", *(d if d else (None, None)))

    def put_key(self, host: str, key: str, body: Any, fetched_at: float | None, key_id: str | None) -> None:
        with self._lock:
            codec, blob = self.encode_body(body)
            self.store.conn(create=True).execute(
                "INSERT OR REPLACE INTO responses (key, host, fetched_at, key_id, codec, body) VALUES (?, ?, ?, ?, ?, ?)",
                (key, host, fetched_at, key_id, codec, blob))

    def put(self, host: str, path: str, params: dict | None, body: Any, fetched_at: float | None,
            key_id: str | None) -> None:
        self.put_key(host, request_key(host, path, params), body, fetched_at, key_id)

    def train_dictionary(self, samples: list[bytes], clock: Callable[[], float] | None = None) -> int | None:
        """A new dictionary from these bodies (canonical JSON); new rows use it. None with too few samples to train."""
        import time

        if len(samples) < MIN_TRAIN_SAMPLES:
            return None
        try:
            zdict = zstandard.train_dictionary(DICT_SIZE, samples)
        except zstandard.ZstdError:
            return None
        with self._lock:
            c = self.store.conn(create=True)
            cur = c.execute("INSERT INTO dictionaries (created_at, data) VALUES (?, ?)",
                            ((clock or time.time)(), zdict.as_bytes()))
            self._writer = False
            return int(cur.lastrowid)

    def rows(self, host: str | None = None) -> Iterator[tuple[str, str, float | None, str | None, str, bytes]]:
        """Every packed row: key, host, fetched_at, key_id, codec, body (still encoded)."""
        c = self.store.conn(create=False)
        if c is None:
            return
        sql = "SELECT key, host, fetched_at, key_id, codec, body FROM responses"
        yield from (c.execute(sql + " WHERE host = ?", (host,)) if host else c.execute(sql))


# ---------- one shared instance per folder ----------
_OPEN: dict[Path, ResponseCache] = {}
_OPEN_LOCK = threading.Lock()


def open_cache(cache_dir: Path) -> ResponseCache:
    key = Path(cache_dir).resolve()
    with _OPEN_LOCK:
        if key not in _OPEN:
            _OPEN[key] = ResponseCache(Path(cache_dir))
        return _OPEN[key]


def close_all() -> None:
    with _OPEN_LOCK:
        for c in _OPEN.values():
            c.close()
        _OPEN.clear()


# ---------- matches (backfill, the refetch estimate): any key's entry, as the files were read ----------
def _match_key(match_id: str) -> tuple[str, str]:
    from draftassist.data.riot import REGION, match_path

    return REGION, request_key(REGION, match_path(match_id), None)


def cached_match(cache_dir: Path, match_id: str) -> dict | None:
    host, key = _match_key(match_id)
    entry = open_cache(cache_dir).get_key(host, key)
    return entry.body if entry is not None else None


def has_match(cache_dir: Path, match_id: str) -> bool:
    return open_cache(cache_dir).has_key(*_match_key(match_id))


def has_matches(cache_dir: Path, match_ids: Iterable[str]) -> int:
    cache = open_cache(cache_dir)
    return sum(cache.has_key(*_match_key(m)) for m in match_ids)
