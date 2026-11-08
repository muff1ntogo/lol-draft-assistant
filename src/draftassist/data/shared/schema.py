"""What is shared, and the guard that nothing identifying is (docs/shared-data.md, hard rules).

Two kinds (Stage 2):
- counts, per (patch, tier, queue): additive (data/counts.py). The runtime tables are derived from them on each machine.
- fits and other non-additive results, per window: published as they are.
Our own data (players, drafts, games, scouting, pools, settings), the per-match crawl rows and anything with PUUIDs or
Riot IDs are never in the registry. Turso holds `shared_blocks` (one row per block) and `publish_log`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from sqlmodel import SQLModel

from draftassist.db import models  # noqa: F401  (registers the tables)

SCHEMA_VERSION = 1  # change whenever a shared table's columns or a block's format change
SOURCES = ("main", "master_plus")  # the main database, and Master+ (its own master.sqlite, data/master.py)


@dataclass(frozen=True)
class SharedTable:
    name: str
    slice_cols: tuple[str, ...]  # one block (or a few parts) per distinct value of these
    kind: str  # "counts" or "fits"

    @cached_property
    def columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in SQLModel.metadata.tables[self.name].columns)

    @cached_property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(c.name for c in SQLModel.metadata.tables[self.name].primary_key.columns)


_COUNT = ("patch", "bracket", "queue")
SHARED_TABLES: tuple[SharedTable, ...] = (
    SharedTable("cnt_bracket", _COUNT, "counts"),
    SharedTable("cnt_role", _COUNT, "counts"),
    SharedTable("cnt_ban", _COUNT, "counts"),
    SharedTable("cnt_matchup", _COUNT, "counts"),
    SharedTable("cnt_pair", _COUNT, "counts"),
    SharedTable("cnt_build", _COUNT, "counts"),
    SharedTable("cnt_order", _COUNT, "counts"),
    SharedTable("cnt_lane", _COUNT, "counts"),
    SharedTable("champion_traits", ("window",), "fits"),
    SharedTable("matchup_fits", ("window",), "fits"),
    SharedTable("synergy_fits", ("window",), "fits"),
    SharedTable("execution_fits", ("window",), "fits"),
    SharedTable("damage_thresholds", ("window",), "fits"),
    SharedTable("power_curves", (), "fits"),
    SharedTable("datasets", (), "fits"),
)
BY_NAME = {t.name: t for t in SHARED_TABLES}
# Runtime tables a pull derives from the counts (data/counts.derive) rather than downloads.
DERIVED_TABLES = ("champ_role_stats", "matchups", "bracket_totals", "synergy_pairs", "synergy_role_stats")

IDENTITY_COLUMN = re.compile(r"puuid|riot_id|game_name|tag_line|summoner", re.IGNORECASE)
_PUUID = re.compile(r"[A-Za-z0-9_-]{70,90}")  # Riot's encrypted PUUIDs are 78 characters of base64url
_RIOT_ID = re.compile(r"\S.{0,30}#[A-Za-z0-9]{2,5}")  # Name#TAG
_MATCH_ID = re.compile(r"[A-Z]{2,4}\d?_\d{6,}")  # EUW1_7123456789


def looks_like_identity(value) -> bool:
    """A value shaped like a PUUID, a Riot ID or a match id. Long text (a fit's JSON) is searched for them too."""
    if not isinstance(value, str):
        return False
    if len(value) <= 100:
        return bool(_PUUID.fullmatch(value) or _RIOT_ID.fullmatch(value) or _MATCH_ID.fullmatch(value))
    return bool(re.search(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{78}(?![A-Za-z0-9_-])", value) or _MATCH_ID.search(value))


def check_identity_free(table: str, columns, rows) -> None:
    for c in columns:
        if IDENTITY_COLUMN.search(c):
            raise ValueError(f"{table}.{c} looks like an identity column: not shared")
    for row in rows:
        for v in row:
            if looks_like_identity(v):
                raise ValueError(f"{table} holds a value shaped like a PUUID, Riot ID or match id: not shared")


REMOTE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS shared_blocks (block_id TEXT PRIMARY KEY, source TEXT NOT NULL, tbl TEXT NOT NULL, "
    "slice TEXT NOT NULL, part INTEGER NOT NULL, parts INTEGER NOT NULL, schema_version INTEGER NOT NULL, "
    "publish_id INTEGER NOT NULL, hash TEXT NOT NULL, codec TEXT NOT NULL, data BLOB, deleted INTEGER NOT NULL DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS shared_blocks_publish ON shared_blocks (publish_id, block_id)",
    "CREATE TABLE IF NOT EXISTS publish_log (publish_id INTEGER PRIMARY KEY, published_at REAL NOT NULL, "
    "app_version TEXT, schema_version INTEGER NOT NULL, dataset_ids TEXT, rows_written INTEGER NOT NULL, windows TEXT)",
)
