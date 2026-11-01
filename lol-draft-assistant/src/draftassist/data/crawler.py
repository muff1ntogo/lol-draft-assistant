"""Crawl current-patch ranked matches for the Iron, Gold and Emerald brackets.

1. Seed players per bracket from league-v4 (Solo/Duo ladder, all four divisions).
2. For each seed (least recently crawled first, round-robin across brackets) pull ranked Solo/Duo
   and Flex match ids from the last two weeks.
3. Fetch each new match; keep it if it is on the current patch and not a remake; store
   participants (champion, team, role, win) and bans.

Resumable: stored matches are skipped, every response is cached on disk, and seeds remember
when they were last crawled. Run it overnight:
    uv run python -m draftassist.data.crawl --hours 8
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.data.riot import RiotClient
from draftassist.db.models import Champion, CrawledMatch, MatchBan, MatchParticipant, SeedPlayer

log = logging.getLogger(__name__)

BRACKETS = ("iron", "gold", "emerald")  # the spec's brackets: our players' plus the Gold enemy prior
LEAGUE_TIERS = ("iron", "bronze", "silver", "gold", "platinum", "emerald", "diamond")  # league-v4 tiers with divisions
DIVISIONS = ("I", "II", "III", "IV")
QUEUES = (420, 440)  # ranked Solo/Duo, ranked Flex
POSITIONS = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "support"}
MIN_DURATION_S = 300  # shorter games are remakes
LOOKBACK_S = 14 * 86_400


def brackets_in_use(db: Engine, enemy_bracket: str = "gold") -> tuple[str, ...]:
    """The spec's brackets plus any bracket a player is set to, in ladder order."""
    from draftassist.db.models import Player

    with Session(db) as s:
        used = {p.bracket for p in s.exec(select(Player)).all()}
    used |= set(BRACKETS) | {enemy_bracket}
    return tuple(t for t in LEAGUE_TIERS if t in used) + tuple(sorted(used - set(LEAGUE_TIERS)))


def patch_of(version: str) -> str:
    """'16.21.1' (Data Dragon) or '16.21.712.4501' (match gameVersion) -> '16.21'."""
    major, minor, *_ = version.split(".")
    return f"{major}.{minor}"


def current_patch(db: Engine) -> str:
    with Session(db) as s:
        versions = s.exec(select(Champion.patch).distinct()).all()
    if not versions:
        raise RuntimeError("champions table is empty: run `uv run python -m draftassist.data.ddragon` first")
    return patch_of(max(versions, key=lambda v: tuple(int(x) for x in v.split("."))))


@dataclass(frozen=True)
class ParsedMatch:
    match_id: str
    patch: str
    queue_id: int
    game_start_ms: int
    participants: tuple[tuple[int, int, str, bool], ...]  # champ, team, role, win
    bans: tuple[tuple[int, int, int], ...]  # team, pick_turn, champ


def parse_match(match: dict) -> ParsedMatch | None:
    """None for remakes, unexpected queues, or games without a clean 5-role layout per team."""
    info = match["info"]
    if info.get("queueId") not in QUEUES or info.get("gameDuration", 0) < MIN_DURATION_S:
        return None
    participants = []
    for p in info["participants"]:
        role = POSITIONS.get(p.get("teamPosition") or "")
        if role is None:
            return None
        participants.append((p["championId"], p["teamId"], role, bool(p["win"])))
    for team in (100, 200):
        if sorted(r for _, t, r, _ in participants if t == team) != sorted(POSITIONS.values()):
            return None
    bans = tuple((t["teamId"], b["pickTurn"], b["championId"]) for t in info["teams"] for b in t.get("bans", []))
    return ParsedMatch(match["metadata"]["matchId"], patch_of(info["gameVersion"]), info["queueId"],
                       info.get("gameStartTimestamp", 0), tuple(participants), bans)


@dataclass
class CrawlStats:
    stored: dict[str, int] = field(default_factory=dict)
    skipped_other_patch: int = 0
    skipped_invalid: int = 0
    players_crawled: int = 0

    @property
    def total_stored(self) -> int:
        return sum(self.stored.values())


class Crawler:
    def __init__(self, riot: RiotClient, db: Engine, patch: str, brackets: Iterable[str] = BRACKETS,
                 clock=time.time):
        self.riot, self.db, self.patch = riot, db, patch
        self.brackets = tuple(brackets)
        self._clock = clock

    def seed(self, pages: int = 1) -> int:
        """Store ladder players for each bracket. Returns the number of new seeds."""
        added = 0
        with Session(self.db) as s:
            known = set(s.exec(select(SeedPlayer.puuid)).all())
            for bracket in self.brackets:
                for division in DIVISIONS:
                    for page in range(1, pages + 1):
                        for e in self.riot.league_entries(bracket.upper(), division, page):
                            puuid = e.get("puuid")
                            if not puuid or puuid in known:
                                continue
                            s.add(SeedPlayer(puuid=puuid, bracket=bracket, tier=e.get("tier", bracket.upper()),
                                             division=e.get("rank", division)))
                            known.add(puuid)
                            added += 1
            s.commit()
        return added

    def _next_players(self, bracket: str, limit: int) -> list[SeedPlayer]:
        with Session(self.db) as s:
            q = (select(SeedPlayer).where(SeedPlayer.bracket == bracket)
                 .order_by(col(SeedPlayer.last_crawled_at).is_not(None), SeedPlayer.last_crawled_at, SeedPlayer.puuid)
                 .limit(limit))
            return list(s.exec(q).all())

    def run(self, max_matches: int | None = None, deadline: float | None = None,
            ids_per_queue: int = 20) -> CrawlStats:
        """Crawl until max_matches are stored, the deadline (epoch seconds) passes, or seeds run out."""
        stats = CrawlStats(stored=dict.fromkeys(self.brackets, 0))
        start_time = int(self._clock() - LOOKBACK_S)

        def done() -> bool:
            return (max_matches is not None and stats.total_stored >= max_matches) or (
                deadline is not None and self._clock() >= deadline)

        queues = {b: self._next_players(b, 10_000) for b in self.brackets}
        while not done() and any(queues.values()):
            for bracket in self.brackets:
                if done() or not queues[bracket]:
                    continue
                player = queues[bracket].pop(0)
                self._crawl_player(player, bracket, start_time, ids_per_queue, stats, done)
        return stats

    def _crawl_player(self, player: SeedPlayer, bracket: str, start_time: int, ids_per_queue: int,
                      stats: CrawlStats, done) -> None:
        ids = [m for q in QUEUES for m in self.riot.match_ids(player.puuid, q, start_time, ids_per_queue)]
        with Session(self.db) as s:
            known = set(s.exec(select(CrawledMatch.match_id).where(col(CrawledMatch.match_id).in_(ids))).all())
        for match_id in ids:
            if done():
                break
            if match_id in known:
                continue
            raw = self.riot.match(match_id)
            parsed = parse_match(raw) if raw else None
            if parsed is None:
                stats.skipped_invalid += 1
                continue
            if parsed.patch != self.patch:
                stats.skipped_other_patch += 1
                continue
            self._store(parsed, bracket)
            known.add(match_id)
            stats.stored[bracket] += 1
        with Session(self.db) as s:
            row = s.get(SeedPlayer, player.puuid)
            row.last_crawled_at = datetime.fromtimestamp(self._clock(), timezone.utc)
            s.add(row)
            s.commit()
        stats.players_crawled += 1

    def _store(self, m: ParsedMatch, bracket: str) -> None:
        with Session(self.db) as s:
            s.add(CrawledMatch(match_id=m.match_id, patch=m.patch, bracket=bracket, queue_id=m.queue_id,
                               game_start_ms=m.game_start_ms))
            s.flush()
            for champ, team, role, win in m.participants:
                s.add(MatchParticipant(match_id=m.match_id, champ_id=champ, team_id=team, role=role, win=win))
            for team, turn, champ in m.bans:
                s.add(MatchBan(match_id=m.match_id, team_id=team, pick_turn=turn, champ_id=champ))
            s.commit()
