"""OP.GG MCP bootstrap: lane meta (win/pick/ban rates per lane) until our own crawl covers a bracket.

One `lol_list_lane_meta_champions` call per lane (5 calls; the "all" response reuses one row class
for every lane, so lanes can't be told apart). It is an all-tier aggregate (the tool has no tier
filter), so the same numbers stand in for every bracket. Responses are cached on disk for a day;
no published rate limits, so keep usage light.

CLI: `uv run python -m draftassist.data.opgg`
"""
from __future__ import annotations

import csv
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, delete, select

from draftassist.db.models import BootstrapRoleStats, Champion

log = logging.getLogger(__name__)

MCP_URL = "https://mcp-api.op.gg/mcp"
CACHE_TTL_S = 86_400
LANES = ("top", "jungle", "mid", "adc", "support")  # OP.GG position names == engine roles


class McpClient:
    """Minimal MCP (streamable HTTP, JSON-RPC) client: initialize, then tools/call."""

    def __init__(self, url: str = MCP_URL, transport: httpx.BaseTransport | None = None):
        self._url = url
        self._http = httpx.Client(timeout=60, transport=transport,
                                  headers={"Accept": "application/json, text/event-stream",
                                           "Content-Type": "application/json"})
        self._id = 0
        self._initialized = False
        self._session: str | None = None

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        headers = {"mcp-session-id": self._session} if self._session else {}
        resp = self._http.post(self._url, json=msg, headers=headers)
        resp.raise_for_status()
        self._session = resp.headers.get("mcp-session-id", self._session)
        if notify:
            return None
        body = resp.text
        if body.lstrip().startswith(("event:", "data:")):
            body = "\n".join(line[5:] for line in body.splitlines() if line.startswith("data:"))
        data = json.loads(body)
        if "error" in data:
            raise RuntimeError(f"OP.GG MCP error: {data['error']}")
        return data["result"]

    def call_tool(self, name: str, arguments: dict) -> str:
        if not self._initialized:
            self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "draftassist", "version": "0.1"}})
            self._rpc("notifications/initialized", notify=True)
            self._initialized = True
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return "".join(c.get("text", "") for c in result.get("content", []))

    def close(self) -> None:
        self._http.close()


def fetch_lane_meta(cache_dir: Path, client: McpClient | None = None, clock=time.time) -> dict[str, str]:
    """Raw lane meta text per lane, from a day-old-at-most disk cache or OP.GG."""
    out: dict[str, str] = {}
    own = client is None
    try:
        for lane in LANES:
            cache = cache_dir / "opgg" / f"lane_meta_{lane}.json"
            if cache.is_file():
                entry = json.loads(cache.read_text(encoding="utf-8"))
                if clock() - entry["fetched_at"] < CACHE_TTL_S:
                    out[lane] = entry["text"]
                    continue
            client = client or McpClient()
            raw = client.call_tool("lol_list_lane_meta_champions", {"lang": "en_US", "position": lane})
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"fetched_at": clock(), "text": raw}), encoding="utf-8")
            out[lane] = raw
    finally:
        if own and client is not None:
            client.close()
    return out


def _value(v: str):
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


HEADER = re.compile(r"^class (\w+): (.+)$", re.MULTILINE)


def parse_lane_meta(raw: str, role: str) -> list[dict]:
    """Parse one lane of OP.GG's compact repr: a `class <Row>: champion,is_rip,play,...` header, then
    rows like `<Row>("Malphite",false,149659,...)`. Returns one dict per champion, tagged with `role`."""
    row_class = next(((cls, fs) for cls, fs in HEADER.findall(raw) if "champion" in fs.split(",")), None)
    if row_class is None:
        raise ValueError("OP.GG lane meta: no row class with a champion field (response format changed?)")
    cls, field_list = row_class
    fields = [f.strip() for f in field_list.split(",")]
    rows = []
    for inner in re.findall(r"\b" + re.escape(cls) + r"\(([^()]*)\)", raw):
        values = next(csv.reader([inner], quotechar='"', skipinitialspace=True))
        if len(values) != len(fields):
            raise ValueError(f"OP.GG lane meta: expected {len(fields)} fields for {cls}, got {len(values)}")
        row = {f: _value(v) for f, v in zip(fields, values)}
        row["role"] = role
        rows.append(row)
    if not rows:
        raise ValueError(f"OP.GG lane meta: no {role} rows found (response format changed?)")
    return rows


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def import_bootstrap(db: Engine, patch: str, rows: list[dict],
                     fetched_at: datetime | None = None) -> tuple[int, list[str]]:
    """Replace the bootstrap rows for `patch`. Returns (rows stored, champion names not matched)."""
    fetched_at = fetched_at or datetime.now(UTC)
    with Session(db) as s:
        lookup = {}
        for c in s.exec(select(Champion)).all():
            lookup[_norm(c.name)] = c.champ_id
            lookup[_norm(c.key)] = c.champ_id
        s.exec(delete(BootstrapRoleStats).where(BootstrapRoleStats.patch == patch))
        stored, unmatched, seen = 0, [], set()
        for r in rows:
            champ = lookup.get(_norm(str(r["champion"])))
            if champ is None:
                unmatched.append(str(r["champion"]))
                continue
            if (champ, r["role"]) in seen:
                log.warning("OP.GG lists %s twice in %s; keeping the first row", r["champion"], r["role"])
                continue
            seen.add((champ, r["role"]))
            s.add(BootstrapRoleStats(patch=patch, champ_id=champ, role=r["role"], games=int(r["play"]),
                                     wins=int(r["win"]), pick_rate=float(r["pick_rate"]),
                                     ban_rate=float(r["ban_rate"]), role_rate=float(r["role_rate"]),
                                     fetched_at=fetched_at))
            stored += 1
        s.commit()
    return stored, sorted(set(unmatched))


def main() -> None:
    from draftassist.config import load_settings
    from draftassist.data.crawler import current_patch
    from draftassist.db.session import init_db

    settings = load_settings()
    db = init_db(settings.db_path)
    patch = current_patch(db)
    raw = fetch_lane_meta(settings.cache_dir)
    rows = [row for lane, text in raw.items() for row in parse_lane_meta(text, lane)]
    stored, unmatched = import_bootstrap(db, patch, rows)
    print(f"OP.GG bootstrap for patch {patch}: {stored} lane rows stored"
          + (f"; unmatched champion names: {', '.join(unmatched)}" if unmatched else ""))


if __name__ == "__main__":
    main()
