"""Tables <-> blocks (decision 2). One block per (source, table, slice): the slice's rows as canonical JSON, gzip, with
the sha256 of the JSON. A slice that compresses past MAX_BLOCK_BYTES is split into parts by rows; all parts carry the
slice's hash, and a pull checks it on the reassembled rows."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Engine, inspect, text

from draftassist.data.shared.schema import SHARED_TABLES, SharedTable, check_identity_free

MAX_BLOCK_BYTES = 1_000_000  # a Turso row stays well under its limits, and a pull's page stays small


@dataclass(frozen=True)
class Slice:
    source: str
    table: str
    key: tuple  # the slice columns' values
    columns: tuple[str, ...]
    rows: list[list]

    @property
    def slice_json(self) -> str:
        return json.dumps(list(self.key), separators=(",", ":"))

    @property
    def slice_key(self) -> str:
        return f"{self.source}|{self.table}|{self.slice_json}"

    def block_id(self, part: int) -> str:
        return f"{self.slice_key}|{part}"


def serialize(columns: Iterable[str], rows: list[list]) -> bytes:
    return json.dumps({"columns": list(columns), "rows": rows}, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(columns, rows) -> str:
    return hashlib.sha256(serialize(columns, rows)).hexdigest()


def encode_parts(columns, rows: list[list], max_bytes: int = MAX_BLOCK_BYTES) -> list[bytes]:
    """gzip blocks of at most max_bytes each (a single row bigger than that is a block on its own)."""
    whole = gzip.compress(serialize(columns, rows), 6)
    if len(whole) <= max_bytes or len(rows) <= 1:
        return [whole]
    n = math.ceil(len(whole) / max_bytes)
    while True:
        size = math.ceil(len(rows) / n)
        parts = [gzip.compress(serialize(columns, rows[i:i + size]), 6) for i in range(0, len(rows), size)]
        if all(len(p) <= max_bytes for p in parts) or size <= 1:
            return parts
        n = math.ceil(n * 1.5)


def decode(data: bytes) -> tuple[list[str], list[list]]:
    payload = json.loads(gzip.decompress(data))
    return payload["columns"], payload["rows"]


def table_slices(engine: Engine | None, source: str, tables: Iterable[SharedTable] = SHARED_TABLES) -> list[Slice]:
    """Every non-empty slice of the shared tables in one database, rows in primary-key order."""
    if engine is None:
        return []
    have = set(inspect(engine).get_table_names())
    out = []
    with engine.connect() as conn:
        for t in tables:
            if t.name not in have:
                continue
            cols = ", ".join(f'"{c}"' for c in t.columns)
            order = ", ".join(f'"{c}"' for c in (*t.slice_cols, *t.primary_key))
            rows = conn.execute(text(f'SELECT {cols} FROM "{t.name}" ORDER BY {order}')).all()
            check_identity_free(t.name, t.columns, rows)
            idx = [t.columns.index(c) for c in t.slice_cols]
            by_slice: dict[tuple, list[list]] = {}
            for r in rows:
                if any(isinstance(v, (bytes, bytearray, memoryview)) for v in r):
                    raise ValueError(f"{t.name}: binary values aren't shared")
                by_slice.setdefault(tuple(r[i] for i in idx), []).append(list(r))
            for key, slice_rows in by_slice.items():
                out.append(Slice(source, t.name, key, t.columns, slice_rows))
    return out
