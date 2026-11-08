"""The shared database behind one small interface (decision 1: Turso's HTTP API over httpx, no SDK).

    execute(sql, args) -> rows         one statement
    batch([(sql, args), ...])          several statements in one transaction: all or none

HttpSharedStore speaks Hrana v2 (`POST <db>/v2/pipeline`); the token is only ever in the Authorization header, never
in repr, logs or errors. FakeSharedStore is the same over an in-memory SQLite, with switches to fail like a network.
"""
from __future__ import annotations

import base64
import sqlite3
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from draftassist.data.shared.schema import REMOTE_SCHEMA

Statement = tuple[str, Sequence[Any]]


class SharedStoreError(RuntimeError):
    def __init__(self, message: str, offline: bool = False):
        super().__init__(message)
        self.offline = offline  # unreachable: keep the last pull, try again later


class SharedStore(Protocol):
    def execute(self, sql: str, args: Sequence[Any] = ()) -> list[tuple]: ...

    def batch(self, statements: Sequence[Statement]) -> None: ...


# ---------- Hrana values ----------
def _arg(v: Any) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode("ascii")}
    return {"type": "text", "value": str(v)}


def _value(v: dict) -> Any:
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":  # Turso sends base64 without the trailing "=" padding (seen on the real database, 8 Nov 2026)
        b64 = v.get("base64") or ""
        return base64.b64decode(b64 + "=" * (-len(b64) % 4))
    return v.get("value")


def http_url(url: str) -> str:
    """libsql://db-org.turso.io (what Turso shows) -> https://db-org.turso.io."""
    url = url.strip().rstrip("/")
    for scheme in ("libsql://", "wss://", "ws://"):
        if url.startswith(scheme):
            return "https://" + url[len(scheme):]
    return url if url.startswith(("https://", "http://")) else "https://" + url


class HttpSharedStore:
    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        self.url = http_url(url)
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {token}"})

    def __repr__(self) -> str:
        return f"HttpSharedStore({self.url})"

    def close(self) -> None:
        self._http.close()

    def _pipeline(self, requests: list[dict]) -> list[dict]:
        try:
            resp = self._http.post(f"{self.url}/v2/pipeline", json={"baton": None,
                                                                     "requests": [*requests, {"type": "close"}]})
        except httpx.TransportError as e:
            raise SharedStoreError(f"shared database unreachable ({type(e).__name__})", offline=True) from None
        if resp.status_code >= 400:
            raise SharedStoreError(f"shared database answered HTTP {resp.status_code}",
                                   offline=resp.status_code >= 500)
        results = resp.json().get("results") or []
        for r in results:
            if r.get("type") == "error":
                raise SharedStoreError((r.get("error") or {}).get("message") or "shared database error")
        return results

    def execute(self, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
        results = self._pipeline([{"type": "execute", "stmt": {"sql": sql, "args": [_arg(a) for a in args]}}])
        result = results[0]["response"]["result"]
        return [tuple(_value(v) for v in row) for row in result.get("rows") or []]

    def batch(self, statements: Sequence[Statement]) -> None:
        steps = [{"stmt": {"sql": "BEGIN"}}]
        for i, (sql, args) in enumerate(statements):
            steps.append({"stmt": {"sql": sql, "args": [_arg(a) for a in args]},
                          "condition": {"type": "ok", "step": i}})
        n = len(steps)
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": n - 1}})
        steps.append({"stmt": {"sql": "ROLLBACK"}, "condition": {"type": "not", "cond": {"type": "ok", "step": n}}})
        results = self._pipeline([{"type": "batch", "batch": {"steps": steps}}])
        result = results[0]["response"]["result"]
        errors = [e for e in (result.get("step_errors") or [])[:-1] if e]
        if errors:
            raise SharedStoreError(errors[0].get("message") or "shared database batch failed")


class FakeSharedStore:
    """In memory, for tests. `rows_written` counts rows written by batches (what Turso bills). Switches: `offline`;
    `fail_writes_after` successful batches; `fail_reads_after` successful reads (raises as unreachable);
    `corrupt_next_block` flips a byte in the next block read."""

    def __init__(self, fail_writes_after: int | None = None):
        self.db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)  # the app's threads
        for sql in REMOTE_SCHEMA:
            self.db.execute(sql)
        self.rows_written = 0
        self.offline = False
        self.fail_writes_after = fail_writes_after
        self.fail_reads_after: int | None = None
        self.corrupt_next_block = False
        self._writes = self._reads = 0

    def _check(self) -> None:
        if self.offline:
            raise SharedStoreError("shared database unreachable (offline)", offline=True)

    def execute(self, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
        self._check()
        if sql.lstrip().upper().startswith("SELECT"):
            if self.fail_reads_after is not None and self._reads >= self.fail_reads_after:
                raise SharedStoreError("shared database unreachable (connection dropped)", offline=True)
            self._reads += 1
        try:
            rows = [tuple(r) for r in self.db.execute(sql, tuple(args)).fetchall()]
        except sqlite3.Error as e:
            raise SharedStoreError(str(e)) from None
        if self.corrupt_next_block and "FROM shared_blocks" in sql:
            for i, r in enumerate(rows):
                for j, v in enumerate(r):
                    if isinstance(v, bytes) and len(v) > 20:
                        rows[i] = (*r[:j], v[:10] + bytes([v[10] ^ 0xFF]) + v[11:], *r[j + 1:])
                        self.corrupt_next_block = False
                        return rows
        return rows

    def batch(self, statements: Sequence[Statement]) -> None:
        self._check()
        if self.fail_writes_after is not None and self._writes >= self.fail_writes_after:
            raise SharedStoreError("shared database unreachable (connection dropped)", offline=True)
        try:
            self.db.execute("BEGIN")
            for sql, args in statements:
                self.db.execute(sql, tuple(args))
            self.db.execute("COMMIT")
        except sqlite3.Error as e:
            self.db.execute("ROLLBACK")
            raise SharedStoreError(str(e)) from None
        self._writes += 1
        self.rows_written += sum(1 for sql, _ in statements
                                 if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")))

    # ---------- test helpers ----------
    def raw_blocks(self) -> list[bytes]:
        import gzip

        return [gzip.decompress(d) for (d,) in self.db.execute("SELECT data FROM shared_blocks WHERE deleted = 0")]

    def decoded_blocks(self) -> list[dict]:
        import json

        out = []
        for tbl, raw in zip([r[0] for r in self.db.execute("SELECT tbl FROM shared_blocks WHERE deleted = 0")],
                            self.raw_blocks()):
            out.append({"table": tbl, **json.loads(raw)})
        return out
