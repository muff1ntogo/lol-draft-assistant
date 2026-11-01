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
    bans: int = 0  # champion-level: matches where it was banned (same on each of its role rows)


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

    puuid: str = Field(primary_key=True)  # "unresolved:<role>" until account-v1 or the client gives the real one
    riot_id: str  # "Name#TAG"
    role: str
    rank: str
    bracket: str

    @property
    def resolved(self) -> bool:
        return not self.puuid.startswith("unresolved:")


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
    draft_format: str | None = None  # tournament | ranked
    result_source: str | None = None  # lcu (end-of-game stats) | match-v5
    match_verified: bool = False  # result and side confirmed against match-v5
    game_length_s: int | None = None
    patch: str | None = None  # major.minor the game was played on
    ddragon_version: str | None = None  # champion art version for that patch, e.g. 16.21.1
    players_json: str | None = None  # our role -> in-game name
    recap_cache: str | None = None  # JSON: lane matchups computed on first view


class DraftAction(SQLModel, table=True):
    __tablename__ = "draft_actions"

    draft_id: int = Field(primary_key=True, foreign_key="drafts.draft_id")
    seq: int = Field(primary_key=True)
    side: str
    type: str  # ban | pick
    champ_id: int  # 0 = skipped ban
    our_rank: int | None = None  # rank of the actual choice among our suggestions
    suggestions_json: str | None = None
    win_prob_after: float | None = None  # model P(win) after this step (backfilled for old games)
    role: str | None = None  # our picks: the role the client gave the player


# ---------- crawl (Phase 3) ----------
class SeedPlayer(SQLModel, table=True):
    """Ranked players per bracket whose match histories feed the crawl."""
    __tablename__ = "seed_players"

    puuid: str = Field(primary_key=True)
    bracket: str = Field(index=True)
    tier: str  # league-v4 tier, e.g. GOLD
    division: str  # I-IV
    last_crawled_at: datetime | None = None  # timezone-aware (UTC)


class CrawledMatch(SQLModel, table=True):
    __tablename__ = "crawled_matches"

    match_id: str = Field(primary_key=True)  # e.g. EUW1_7123456789
    patch: str = Field(index=True)  # major.minor, e.g. 16.21
    bracket: str = Field(index=True)  # bracket of the seed player it was found through
    queue_id: int
    game_start_ms: int


class MatchParticipant(SQLModel, table=True):
    __tablename__ = "match_participants"

    match_id: str = Field(primary_key=True, foreign_key="crawled_matches.match_id")
    champ_id: int = Field(primary_key=True)
    team_id: int  # 100 blue, 200 red
    role: str  # engine role
    win: bool


class MatchBan(SQLModel, table=True):
    __tablename__ = "match_bans"

    match_id: str = Field(primary_key=True, foreign_key="crawled_matches.match_id")
    team_id: int = Field(primary_key=True)
    pick_turn: int = Field(primary_key=True)
    champ_id: int  # -1 when no ban


class BracketTotal(SQLModel, table=True):
    """Matches aggregated per patch and bracket: the denominator for pick and ban rates."""
    __tablename__ = "bracket_totals"

    patch: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    matches: int = 0


class BootstrapRoleStats(SQLModel, table=True):
    """OP.GG lane meta (all-tier aggregate), used for a bracket until our crawl covers it."""
    __tablename__ = "bootstrap_role_stats"

    patch: str = Field(primary_key=True)
    champ_id: int = Field(primary_key=True)
    role: str = Field(primary_key=True)
    games: int
    wins: int
    pick_rate: float
    ban_rate: float
    role_rate: float  # share of the champion's games played in this role
    source: str = "opgg"
    fetched_at: datetime  # timezone-aware (UTC)


class AppSetting(SQLModel, table=True):
    """Small JSON settings edited on the Settings page (e.g. ban value weights)."""
    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: str  # JSON
