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
    tags: list[str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))  # classes: Tank, Mage, ...
    attack_range: float | None = None  # Data Dragon stats.attackrange (ranged / ranged_long traits)


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
    # Damage profile: mean per-game share of damage to champions (None until damage is crawled)
    dmg_phys: float | None = None
    dmg_magic: float | None = None
    dmg_true: float | None = None
    # Power curve (docs/comp-style.md): games and wins in short (< 25 min) and long (> 32 min) games
    games_short: int | None = None
    wins_short: int | None = None
    games_long: int | None = None
    wins_long: int | None = None


class Matchup(SQLModel, table=True):
    __tablename__ = "matchups"

    patch: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    role: str = Field(primary_key=True)
    champ_a: int = Field(primary_key=True)
    champ_b: int = Field(primary_key=True)
    games: int = 0
    wins_a: int = 0
    # docs/matchup-archetypes.md, Stage 4: a's gold and XP leads at 15 minutes, summed, over lane_n games
    lane_gold_sum: float = 0.0
    lane_xp_sum: float = 0.0
    lane_n: int = 0


class SynergyPair(SQLModel, table=True):
    """Role-aware same-team duos over a pooled, time-decayed window (docs/synergy-visualizer.md).
    `window` names the newest patch in the window; `bracket` is a tier or "all" (every tier pooled).
    Each pair is stored once, with (role_a, champ_a) < (role_b, champ_b)."""
    __tablename__ = "synergy_pairs"

    window: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    role_a: str = Field(primary_key=True)
    champ_a: int = Field(primary_key=True)
    role_b: str = Field(primary_key=True)
    champ_b: int = Field(primary_key=True)
    games: int = 0  # raw count (the low-data flag)
    games_w: float = 0.0  # time-decay weighted
    wins_w: float = 0.0


class SynergyRoleStats(SQLModel, table=True):
    """Champion strength per role over the same window as synergy_pairs, so it is removed consistently."""
    __tablename__ = "synergy_role_stats"

    window: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    champ_id: int = Field(primary_key=True)
    role: str = Field(primary_key=True)
    games: int = 0
    games_w: float = 0.0
    wins_w: float = 0.0


class Player(SQLModel, table=True):
    __tablename__ = "players"

    puuid: str = Field(primary_key=True)  # internal key: "unresolved:<role>[:n]" until linked, then one of the ids below
    riot_id: str  # "Name#TAG"
    role: str
    rank: str
    bracket: str
    # The League client and the public Riot API use different PUUIDs for the same account: the client's
    # is a plain UUID, the API's is encrypted per API key. Champ select needs the first, match-v5 the second.
    client_puuid: str | None = None
    api_puuid: str | None = None
    # One active player per role; a swapped-out player stays on the bench with their pool (docs/player-roster.md)
    active: bool = True

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
    plan_json: str | None = None  # JSON: the plan pinned for this game (docs/synergy-v2.md, Stage 6)
    timeline_version: int = 0  # data/timelines.TIMELINE_VERSION: own_game_participants filled, or the game has none
    imported: bool = False  # from match-v5 afterwards, not watched live: no suggestions, pick order approximate


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
    role: str | None = None  # our picks: the role the client gave the player; imported games: both teams' (match-v5)


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
    game_duration_s: int | None = None  # None for matches stored before it was recorded (data.backfill)
    timeline_done: bool = False  # data/timelines.py: leads stored, or there are none
    timeline_version: int = 0  # data/timelines.TIMELINE_VERSION: everything is stored, or the game has none


class ParticipantTimeline(SQLModel):
    """What one player's timeline gives (data/timeline_map.py; docs/draft-vs-execution.md, Stage 1). None = unknown, stored
    before it was recorded, or the game ended before 15:00."""
    lane_gold_15: float | None = None  # lead over the enemy in the same position at 15 minutes
    lane_xp_15: float | None = None
    lane_cs_14: float | None = None  # minions + jungle minions lead at 14:00
    cs_15: float | None = None
    jungle_cs_10: float | None = None
    jungle_cs_15: float | None = None
    level_15: float | None = None
    deaths_solo_14: float | None = None  # before 14:00; the killer had no assists
    deaths_ganked_14: float | None = None  # before 14:00; 2+ enemies, one from outside the lane (ADC + support = one lane)
    deaths_other_14: float | None = None
    kp_15: float | None = None  # (kills + assists) / team kills before 15:00; None if the team had none
    plates_14: float | None = None  # credited to this player before 14:00; a third of plates credit nobody
    wards_placed_15: float | None = None  # real wards, not the ability objects typed UNDEFINED
    wards_killed_15: float | None = None


class MatchParticipant(ParticipantTimeline, table=True):
    __tablename__ = "match_participants"

    match_id: str = Field(primary_key=True, foreign_key="crawled_matches.match_id")
    champ_id: int = Field(primary_key=True)
    team_id: int  # 100 blue, 200 red
    role: str  # engine role
    win: bool
    dmg_phys: int | None = None  # physical / magic / true damage dealt to champions
    dmg_magic: int | None = None
    dmg_true: int | None = None
    # Trait sources (docs/synergy-v2.md, Stage 1; field mapping in data/traits_map.py). None = not in the
    # match JSON (challenges keys vary by patch) or stored before they were recorded (data.backfill).
    cc_time: float | None = None  # timeCCingOthers, seconds
    immobilizations: float | None = None  # challenges.enemyChampionImmobilizations
    dmg_taken_share: float | None = None  # share of the team's damage taken
    self_mitigated: float | None = None  # damageSelfMitigated
    heal_shield: float | None = None  # heals + shields on teammates
    lane_adv: float | None = None  # challenges.laningPhaseGoldExpAdvantage (or the early-laning key): only 0 or 1


class OwnGameParticipant(ParticipantTimeline, table=True):
    """Our logged games, all ten players. Not in match_participants: aggregation reads that, and these aren't crawl data."""
    __tablename__ = "own_game_participants"

    draft_id: int = Field(primary_key=True, foreign_key="drafts.draft_id")
    champ_id: int = Field(primary_key=True)
    match_id: str = Field(index=True)
    team_id: int
    role: str | None = None  # from the match; None if Riot didn't give a position


class MatchTeamState(SQLModel, table=True):
    """One team's game state, for crawled matches and our own games alike (no foreign key: ours aren't in crawled_matches).
    Nothing aggregates it. None = unknown, or gold_25 in a game that ended first."""
    __tablename__ = "match_team_state"

    match_id: str = Field(primary_key=True)
    team_id: int = Field(primary_key=True)
    gold_15: float | None = None
    kills_15: float | None = None
    towers_15: float | None = None
    plates_15: float | None = None
    dragons_15: float | None = None
    grubs_15: float | None = None
    herald_15: float | None = None
    gold_25: float | None = None
    dragons_after_15: float | None = None
    barons: float | None = None
    towers_after_15: float | None = None
    deaths_while_ahead: float | None = None  # after 15:00, with a lead of 2,000+ at the previous minute frame


class MatchDraftLogOdds(SQLModel, table=True):
    """The draft model's view of a crawled match, from blue's side (docs/draft-vs-execution.md, Stage 2). A batch job: cached."""
    __tablename__ = "match_draft_logodds"

    match_id: str = Field(primary_key=True)
    window: str  # the fit it came from; another window is recomputed
    value: float


class ExecutionFit(SQLModel, table=True):
    """The fitted expectations and state model, as JSON (data/execution_model.py). The latest fit is the one read."""
    __tablename__ = "execution_fits"

    window: str = Field(primary_key=True)
    body: str


class GameExecution(SQLModel, table=True):
    """One logged game split into draft, early game and conversion (learning/execution.py). All from our team's view, as
    fractions of a win. Only the draft part without a timeline: p15, early, conversion and verdict stay None."""
    __tablename__ = "game_execution"

    draft_id: int = Field(primary_key=True, foreign_key="drafts.draft_id")
    window: str  # the fit it came from
    p0: float
    p15: float | None = None
    draft: float
    early: float | None = None
    conversion: float | None = None
    verdict: str | None = None
    contributors_json: str | None = None  # JSON: the verdict's top two
    detail_json: str | None = None  # JSON: what happened after 15:00, each as [ours, theirs], and gold at 25


class GameExecutionRole(SQLModel, table=True):
    """A role's part of the early game: top, jungle, mid, bot (adc + support), adc, support, objectives (what's left)."""
    __tablename__ = "game_execution_roles"

    draft_id: int = Field(primary_key=True, foreign_key="drafts.draft_id")
    role: str = Field(primary_key=True)
    share: float
    label: str
    headline_json: str  # JSON: the stat line the label comes from
    stats_json: str  # JSON: the stat lines shown for this role


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


class PlayerGame(SQLModel, table=True):
    """One of our players' recent games (comfort scoring): from match-v5 history or our own logs."""
    __tablename__ = "player_games"

    puuid: str = Field(primary_key=True)  # players.puuid (a placeholder until the player is linked)
    match_id: str = Field(primary_key=True)  # EUW1_<gameId>, or draft-<id> for a logged game without one
    champ_id: int
    role: str | None
    win: bool
    queue_id: int | None = None
    game_start_ms: int  # epoch ms
    source: str  # match-v5 | logged


class ScoutPlayer(SQLModel, table=True):
    """An enemy player we've scouted (docs/enemy-scouting.md). Local only: holds a PUUID, never committed."""
    __tablename__ = "scout_players"

    riot_id: str = Field(primary_key=True)  # "Name#TAG", as typed or as the client gave it; matched case-insensitively
    api_puuid: str | None = None  # None: no such account
    mastery_json: str | None = None  # JSON: [[champ_id, points], ...], highest first
    status: str = "new"  # new | mastery (pool from mastery only) | games (history being read) | done | missing | error
    games_wanted: int = 0  # match ids found for the window
    fetched_at: float = 0.0  # epoch seconds, when the last fetch finished or started


class ScoutGame(SQLModel, table=True):
    """One of a scouted player's recent ranked games."""
    __tablename__ = "scout_games"

    api_puuid: str = Field(primary_key=True)
    match_id: str = Field(primary_key=True)
    champ_id: int
    role: str | None
    win: bool
    queue_id: int | None = None
    game_start_ms: int


class ChampionTraitRow(SQLModel, table=True):
    """Measured traits per champion and role over the synergy window (docs/synergy-v2.md, Stage 1).
    Percentiles within the role (0-1), except the damage shares (real shares) and ranged (0/1).
    None = the source field is missing or the champion has too few games."""
    __tablename__ = "champion_traits"

    window: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    champ_id: int = Field(primary_key=True)
    role: str = Field(primary_key=True)
    games: int = 0
    games_w: float = 0.0
    cc: float | None = None
    frontline: float | None = None
    peel: float | None = None
    dmg_phys: float | None = None
    dmg_magic: float | None = None
    dmg_true: float | None = None
    power_curve: float | None = None  # percentile of the slope (high = scaling)
    power_curve_slope: float | None = None  # log-odds per game-length bucket: negative = early
    lane_pressure: float | None = None
    ranged: float | None = None
    # percentiles across every role (team rules)
    cc_all: float | None = None
    frontline_all: float | None = None
    peel_all: float | None = None


class ChampionTag(SQLModel, table=True):
    """Judgement traits rated 0-3 per champion and role, loaded from data/champion_tags.yaml (the source
    of truth, edited on the Tags page). Unreviewed tags count at half weight."""
    __tablename__ = "champion_tags"

    champ_id: int = Field(primary_key=True)
    role: str = Field(primary_key=True)
    engage: int = 0
    disengage: int = 0
    poke: int = 0
    dive: int = 0
    pick: int = 0
    splitpush: int = 0
    enchanter: int = 0
    hypercarry: int = 0
    setup: int = 0  # Stage 6: starts fights with independent CC
    followup: int = 0  # ends or cleans up fights
    needs_team: int = 0  # a win condition only with team follow-up
    archetype: str | None = None  # docs/matchup-archetypes.md, Stage 1
    archetype2: str | None = None
    archetype_source: str | None = None
    kit: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))  # kit tag -> 1..3 (Stage 2)
    reviewed: bool = False  # the 0-3 synergy tags
    reviewed_kit: bool = False
    reviewed_arch: bool = False


class MatchupFit(SQLModel, table=True):
    """Fitted matchup model (docs/matchup-archetypes.md, Stage 3), refitted by aggregation. param: `E:a>b` or `K:g*h`
    (log-odds, pooled over patches and brackets) or `k_r` (the role's shrinkage in games)."""
    __tablename__ = "matchup_fits"

    window: str = Field(primary_key=True)
    role: str = Field(primary_key=True)
    param: str = Field(primary_key=True)
    value: float = 0.0
    prior_pp: float | None = None
    support: int = 0  # weighted games the role was fitted on


class SynergyFit(SQLModel, table=True):
    """Fitted synergy v2 rules (docs/synergy-v2.md, Stage 2), refitted by aggregation and on prior edits.
    model: style | team (coefficients, log-odds, pooled over brackets) | team_side (blue-side intercept;
    support = matches) | team_mean (per bracket: the average comp's feature, for Team = f(comp) - f(avg))."""
    __tablename__ = "synergy_fits"

    window: str = Field(primary_key=True)
    bracket: str = Field(primary_key=True)
    model: str = Field(primary_key=True)
    rule_id: str = Field(primary_key=True)
    prior_pp: float | None = None  # the prior the fit used
    value: float = 0.0
    support: int = 0  # style: games of pairs the rule applies to; team: matches where it applied
