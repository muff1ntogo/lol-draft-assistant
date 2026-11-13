"""A player's profile figures (docs/roster-profiles.md, Stage A).

Everything a profile shows that comes from stored rows: the summary on the Roster page and the sections of the profile
page. The functions up to `profile()` are pure (plain records in, dicts out), so they are tested on planted games;
`load_inputs()` reads the records for one player from the database. What needs the engine (blind safety, pool health,
the execution hint) is added by the API layer.

Figures only count games that carry them: a logged game whose match was never read has no KDA, so each block says how
many games it is from (`stat_games`).
"""
from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.riot import PLATFORM
from draftassist.db.models import (
    Draft,
    GameExecutionRole,
    Player,
    PlayerGame,
    PoolEntry,
)
from draftassist.engine.model import ROLES
from draftassist.learning.comfort import WINDOW_DAYS
from draftassist.learning.trends import band

DAY_MS = 86_400_000
WINDOWS = ("20", "60d")  # the profile's window picker: the last 20 games, or the last 60 days
LAST_GAMES = 20
MIN_EXECUTION_GAMES = 5  # "Needs 5 team games"
TREND_GAMES = 10  # the summary's trend arrow reads the last 10 team games: the newer 5 against the older 5
TREND_FLAT_PP = 1.0  # a change under this is no trend
STRIP_CHAMPS = 5
ALSO_PLAYS_SHARE = 0.15
MASTERY_TOP = 10
TIER_ORDER = {"main": 0, "comfortable": 1, "learning": 2}
# The comfort-weighted pool size: a main counts as a whole champion, a comfortable pick as half, one being learnt as a quarter
TIER_WEIGHT = {"main": 1.0, "comfortable": 0.5, "learning": 0.25}
RANK_TIERS = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER")
RANK_DIVISIONS = ("IV", "III", "II", "I")
RANK_QUEUES = {"RANKED_SOLO_5x5": "solo", "RANKED_FLEX_SR": "flex", "RANKED_PREMADE_5x5": "fives"}  # fives: Ranked 5s
BEST_OF = ("solo", "flex")  # the rank on a Roster row: the higher of these two (the 5s ladder is its own scale)


@dataclass(frozen=True)
class Game:
    """One of the player's games (a `player_games` row)."""
    match_id: str
    champ_id: int
    role: str | None
    win: bool
    start_ms: int
    source: str = "match-v5"
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    cs: int | None = None
    vision: int | None = None
    duration_s: int | None = None

    @property
    def has_stats(self) -> bool:
        return self.kills is not None and self.deaths is not None and self.assists is not None


@dataclass(frozen=True)
class PoolRow:
    champ_id: int
    role: str
    tier: str
    scored_tier: str | None = None
    locked: bool = False


@dataclass(frozen=True)
class ExecutionRow:
    """The player's part of one team game's early game (`game_execution_roles` for the role they played)."""
    start_ms: int
    role: str
    share: float  # a fraction of a win, as stored
    label: str
    champ_id: int | None = None


# ---------- games ----------
def in_window(games: Iterable[Game], window: str, now_ms: int) -> list[Game]:
    """The games a window covers, newest first: "20" = the last 20 inside the 60 days, "60d" = all of the 60 days."""
    recent = sorted((g for g in games if g.start_ms >= now_ms - WINDOW_DAYS * DAY_MS), key=lambda g: -g.start_ms)
    return recent[:LAST_GAMES] if window == "20" else recent


def figures(games: Sequence[Game]) -> dict:
    """Games, win rate, KDA, CS a minute and vision a minute. KDA is (kills + assists) / deaths over the games that
    have them; with no deaths at all it is kills + assists."""
    n = len(games)
    wins = sum(g.win for g in games)
    with_stats = [g for g in games if g.has_stats]
    k, d, a = (sum(getattr(g, f) for g in with_stats) for f in ("kills", "deaths", "assists"))
    timed = [g for g in with_stats if g.duration_s and g.cs is not None]
    minutes = sum(g.duration_s for g in timed) / 60
    seen = [g for g in with_stats if g.duration_s and g.vision is not None]
    seen_minutes = sum(g.duration_s for g in seen) / 60
    return {
        "games": n, "wins": wins, "win_rate": wins / n if n else None,
        "kda": (k + a) / max(d, 1) if with_stats else None,
        "kills": k / len(with_stats) if with_stats else None, "deaths": d / len(with_stats) if with_stats else None,
        "assists": a / len(with_stats) if with_stats else None,
        "cs_min": sum(g.cs for g in timed) / minutes if minutes else None,
        "vision_min": sum(g.vision for g in seen) / seen_minutes if seen_minutes else None,
        "stat_games": len(with_stats),
    }


def roles_breakdown(games: Sequence[Game]) -> list[dict]:
    """Their games by role, most played first, with each role's share of the games that have a role."""
    placed = [g for g in games if g.role]
    out = []
    for role in ROLES:
        mine = [g for g in placed if g.role == role]
        if mine:
            wins = sum(g.win for g in mine)
            out.append({"role": role, "games": len(mine), "wins": wins, "win_rate": wins / len(mine), "share": len(mine) / len(placed)})
    return sorted(out, key=lambda r: -r["games"])


def also_plays(games: Sequence[Game], role: str) -> list[dict]:
    """Other roles with at least 15% of their games: whether they are really a flex player."""
    return [r for r in roles_breakdown(games) if r["role"] != role and r["share"] >= ALSO_PLAYS_SHARE]


# ---------- lane execution ----------
def execution_figures(rows: Sequence[ExecutionRow]) -> dict:
    """The player's lane execution over their team games: the mean share (pp of a win) with its band, how often each
    label came up, the last 20 shares (oldest first, for a line) and a trend over the last 10. Below 5 games there are
    no numbers: single games are noisy."""
    ordered = sorted(rows, key=lambda r: r.start_ms)
    n = len(ordered)
    out: dict = {"games": n, "needed": MIN_EXECUTION_GAMES, "enough": n >= MIN_EXECUTION_GAMES, "mean_pp": None, "se_pp": None,
                 "label": None, "labels": {}, "last": [], "trend": None}
    if n < MIN_EXECUTION_GAMES:
        return out
    shares = [100 * r.share for r in ordered]
    b = band(shares)
    out.update(mean_pp=b.mean, se_pp=b.se, labels=dict(Counter(r.label for r in ordered).most_common()), last=shares[-LAST_GAMES:],
               label="Even" if not b.excludes_zero else "Wins lane" if b.mean > 0 else "Loses lane")
    if n >= TREND_GAMES:
        last = shares[-TREND_GAMES:]
        half = TREND_GAMES // 2
        change = sum(last[half:]) / half - sum(last[:half]) / half
        out["trend"] = "flat" if abs(change) < TREND_FLAT_PP else "up" if change > 0 else "down"
    return out


# ---------- rank and mastery ----------
def _rank_key(e: Mapping) -> tuple[int, int, int]:
    tier = str(e.get("tier", "")).upper()
    division = str(e.get("rank", "")).upper()
    return (RANK_TIERS.index(tier) if tier in RANK_TIERS else -1,
            RANK_DIVISIONS.index(division) if division in RANK_DIVISIONS else 0, int(e.get("leaguePoints") or 0))


def rank_summary(entries: Iterable[Mapping] | None) -> dict:
    """league-v4 entries -> {"solo": {...}, "flex": {...}, "fives": {...}, "best": the higher of solo and flex}. Each:
    label ("Gold II"), lp, wins, losses, queue."""
    out: dict = {"solo": None, "flex": None, "fives": None, "best": None}
    best = None
    for e in entries or ():
        queue = RANK_QUEUES.get(e.get("queueType"))
        if queue is None or str(e.get("tier", "")).upper() not in RANK_TIERS:
            continue
        tier = str(e["tier"]).title()
        apex = str(e["tier"]).upper() in ("MASTER", "GRANDMASTER", "CHALLENGER")
        row = {"queue": queue, "label": tier if apex else f"{tier} {e.get('rank', '')}".strip(), "lp": int(e.get("leaguePoints") or 0),
               "wins": int(e.get("wins") or 0), "losses": int(e.get("losses") or 0)}
        out[queue] = row
        if queue in BEST_OF and (best is None or _rank_key(e) > best[0]):
            best = (_rank_key(e), row)
    out["best"] = best[1] if best else None
    return out


def mastery_top(mastery: Sequence[Sequence[int]] | None, pool_champs: Iterable[int], n: int = MASTERY_TOP) -> list[dict]:
    """The top champions by mastery points, with when they were last played and whether they are in the pool."""
    pool = set(pool_champs)
    rows = sorted(mastery or (), key=lambda m: -m[1])[:n]
    return [{"champ_id": c, "points": pts, "last_played_ms": last or None, "in_pool": c in pool} for c, pts, last in rows]


# ---------- the pool ----------
def pool_strip(pool: Sequence[PoolRow], role: str, n: int = STRIP_CHAMPS) -> dict:
    """The pool for the row on the Roster page: the first champions in tier order, and how many more there are."""
    mine = sorted((e for e in pool if e.role == role), key=lambda e: (TIER_ORDER.get(e.tier, 9), e.champ_id))
    return {"champs": [{"champ_id": e.champ_id, "tier": e.tier} for e in mine[:n]], "more": max(0, len(mine) - n), "size": len(mine)}


def pool_size_weighted(pool: Sequence[PoolRow], role: str) -> float:
    return sum(TIER_WEIGHT.get(e.tier, 0.0) for e in pool if e.role == role)


def pending_changes(pool: Sequence[PoolRow], role: str) -> int:
    """Tier changes the comfort re-scoring suggests and nobody has accepted or locked."""
    return sum(1 for e in pool if e.role == role and e.scored_tier and e.scored_tier != e.tier and not e.locked)


def champion_rows(games: Sequence[Game], pool: Sequence[PoolRow], role: str, mastery: Sequence[Sequence[int]] | None = None) -> dict:
    """The pool table for one role: every pool champion (tier, then games), and under it what they played in the role
    that isn't in the pool, so the pool can catch up with what they actually play."""
    points = {m[0]: m[1] for m in mastery or ()}
    by_champ: dict[int, list[Game]] = {}
    for g in games:
        if g.role == role:
            by_champ.setdefault(g.champ_id, []).append(g)

    def row(champ_id: int, entry: PoolRow | None) -> dict:
        mine = by_champ.get(champ_id, [])
        f = figures(mine)
        return {"champ_id": champ_id, "in_pool": entry is not None, "tier": entry.tier if entry else None,
                "locked": entry.locked if entry else False,
                "suggested_tier": entry.scored_tier if entry and entry.scored_tier != entry.tier and not entry.locked else None,
                "games": f["games"], "wins": f["wins"], "win_rate": f["win_rate"], "kda": f["kda"], "cs_min": f["cs_min"],
                "stat_games": f["stat_games"], "last_played_ms": max((g.start_ms for g in mine), default=None),
                "mastery": points.get(champ_id)}

    entries = [e for e in pool if e.role == role]
    in_pool = sorted((row(e.champ_id, e) for e in entries), key=lambda r: (TIER_ORDER.get(r["tier"], 9), -r["games"], r["champ_id"]))
    have = {e.champ_id for e in entries}
    others = sorted((row(c, None) for c in by_champ if c not in have), key=lambda r: (-r["games"], r["champ_id"]))
    return {"role": role, "pool": in_pool, "not_in_pool": others}


# ---------- what the pages read ----------
def summary(games: Sequence[Game], pool: Sequence[PoolRow], execution: Sequence[ExecutionRow], role: str,
            rank: Iterable[Mapping] | None = None, now_ms: int | None = None) -> dict:
    """The figures in a Roster row: 60 days in this role."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    mine = [g for g in in_window(games, "60d", now_ms) if g.role == role]
    ex = execution_figures([r for r in execution if r.role == role])
    return {"role": role, **figures(mine), "pool": pool_strip(pool, role), "pending": pending_changes(pool, role),
            "rank": rank_summary(rank)["best"],
            "execution": {k: ex[k] for k in ("games", "needed", "enough", "mean_pp", "label", "trend")},
            "last_played_ms": max((g.start_ms for g in games), default=None)}


def profile(games: Sequence[Game], pool: Sequence[PoolRow], execution: Sequence[ExecutionRow], role: str,
            mastery: Sequence[Sequence[int]] | None = None, rank: Iterable[Mapping] | None = None, window: str = "60d",
            now_ms: int | None = None, plays: Iterable[str] = ()) -> dict:
    """Every section of the profile page that comes from stored rows. `window` picks the games the figures read; the
    pool roles are the player's role first, then every other role they play (`plays`) or have a pool in."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}")
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    chosen = in_window(games, window, now_ms)
    in_role = [g for g in chosen if g.role == role]
    ex = execution_figures([r for r in execution if r.role == role])
    plays = set(plays)
    pool_roles = [role] + [r for r in ROLES if r != role and (r in plays or any(e.role == r for e in pool))]
    return {
        "role": role, "window": window, "windows": list(WINDOWS),
        "tiles": {**figures(in_role), "execution": {k: ex[k] for k in ("games", "needed", "enough", "mean_pp", "se_pp", "label", "trend")},
                  "pool_size": pool_size_weighted(pool, role), "pool_champs": sum(1 for e in pool if e.role == role)},
        "pools": [champion_rows(chosen, pool, r, mastery) for r in pool_roles],
        "roles": roles_breakdown(chosen),
        "execution": ex,
        "mastery": mastery_top(mastery, (e.champ_id for e in pool)),
        "rank": rank_summary(rank),
        "pending": pending_changes(pool, role),
    }


# ---------- reading one player's records ----------
@dataclass(frozen=True)
class Inputs:
    games: list[Game]
    pool: list[PoolRow]
    execution: list[ExecutionRow]
    mastery: list[list[int]] | None
    rank: list[dict] | None


def load_inputs(db: Engine, puuid: str) -> Inputs:
    """One player's games, pool, lane execution rows, mastery and rank. A team game is theirs in a role when
    `player_games` has it under their key in that role (logged games are filed under whoever was active)."""
    with Session(db) as s:
        player = s.get(Player, puuid)
        rows = s.exec(select(PlayerGame).where(PlayerGame.puuid == puuid)).all()
        games = [Game(g.match_id, g.champ_id, g.role, g.win, g.game_start_ms, g.source, g.kills, g.deaths, g.assists, g.cs,
                      g.vision, g.duration_s) for g in rows]
        pool = [PoolRow(e.champ_id, e.role, e.manual_tier, e.scored_tier, e.tier_locked)
                for e in s.exec(select(PoolEntry).where(PoolEntry.puuid == puuid)).all()]
        by_match = {g.match_id: g for g in rows if g.role}
        drafts: dict[int, PlayerGame] = {}
        if by_match:
            prefix = f"{PLATFORM.upper()}_"
            for d in s.exec(select(Draft)).all():
                g = by_match.get(f"{prefix}{d.game_id}") if d.game_id else None
                g = g or by_match.get(f"draft-{d.draft_id}")
                if g is not None:
                    drafts[d.draft_id] = g
        execution = []
        if drafts:
            for r in s.exec(select(GameExecutionRole).where(GameExecutionRole.draft_id.in_(drafts))).all():  # type: ignore[attr-defined]
                g = drafts[r.draft_id]
                if r.role == g.role:
                    execution.append(ExecutionRow(g.game_start_ms, r.role, r.share, r.label, g.champ_id))
        mastery = json.loads(player.mastery_json) if player and player.mastery_json else None
        rank = json.loads(player.rank_json) if player and player.rank_json else None
    return Inputs(games, pool, execution, mastery, rank)
