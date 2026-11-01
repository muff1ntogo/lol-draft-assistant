"""SQLite tables (see docs/SPEC.md, Data model). Stats tables are keyed by patch and bracket.

`enemy_scout` is v2 and not defined yet.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class Champion(SQLModel, table=True):
    __tablename__ = "champions"

    champ_id: int = Field(primary_key=True)  # Data Dragon numeric `key`, e.g. 266
    key: str = Field(index=True)  # Data Dragon `id`, e.g. "MonkeyKing"
    name: str  # display name, e.g. "Wukong"
    roles: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    patch: str = Field(index=True)  # Data Dragon version this row was loaded from


class ChampRoleStats(SQLModel, table=True):
    __tablename__ = "champ_role_stats"

    patch: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    champ_id: int = Field(primary_key=True)
    role: str = Field(primary_key=True)
    games: int = 0
    wins: int = 0
    picks: int = 0
    bans: int = 0


class Matchup(SQLModel, table=True):
    __tablename__ = "matchups"

    patch: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    role: str = Field(primary_key=True)
    champ_a: int = Field(primary_key=True)
    champ_b: int = Field(primary_key=True)
    games: int = 0
    wins_a: int = 0


class Synergy(SQLModel, table=True):
    __tablename__ = "synergy"

    patch: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    champ_a: int = Field(primary_key=True)
    champ_b: int = Field(primary_key=True)
    games: int = 0
    wins: int = 0


class Player(SQLModel, table=True):
    __tablename__ = "players"

    puuid: str = Field(primary_key=True)
    riot_id: str  # "Name#TAG"
    role: str
    rank: str
    bracket: str


class PoolEntry(SQLModel, table=True):
    __tablename__ = "pool_entries"

    puuid: str = Field(primary_key=True, foreign_key="players.puuid")
    champ_id: int = Field(primary_key=True)
    role: str = Field(primary_key=True)
    manual_tier: str  # main | comfortable | learning
    scored_tier: str | None = None
    tier_locked: bool = False
    comfort: float = 0.0
    games: int = 0
    wins: int = 0
    last_played: datetime | None = None  # timezone-aware (UTC)


class Draft(SQLModel, table=True):
    __tablename__ = "drafts"

    draft_id: int | None = Field(default=None, primary_key=True)
    game_id: int | None = Field(default=None, index=True)
    queue_id: int | None = None
    side: str  # blue | red
    played_at: datetime  # timezone-aware (UTC)
    result: str | None = None  # win | loss | None while unknown
    pred_win_prob: float | None = None
    style_tag: str | None = None


class DraftAction(SQLModel, table=True):
    __tablename__ = "draft_actions"

    draft_id: int = Field(primary_key=True, foreign_key="drafts.draft_id")
    seq: int = Field(primary_key=True)
    side: str
    type: str  # ban | pick
    champ_id: int  # 0 = skipped ban
    our_rank: int | None = None  # rank of the actual choice among our suggestions
    suggestions_json: str | None = None
