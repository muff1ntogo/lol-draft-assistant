"""Reading a new player before they are added (docs/roster-profiles.md, Add player, steps 1-2).

A lookup is one account-v1 request. An onboarding job then reads the player's ranked games of the last 60 days, their
mastery and their rank in a background thread, and turns them into a pool proposal. Nothing is stored until the user
confirms the reviewed pool (`data/pools.create_player`), so jobs live in memory only and a closed sheet costs nothing.
While it reads, a running crawl is asked to leave it the key's rate limit, as scouting does (`cache/jobs/yield.json`).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from draftassist.data.crawler import POSITIONS
from draftassist.data.progress import request_yield
from draftassist.data.riot import RiotAuthError, RiotUnavailable
from draftassist.learning.comfort import WINDOW_DAYS
from draftassist.learning.history import (
    MAX_MATCHES_PER_PLAYER,
    MIN_DURATION_S,
    game_stats,
    mastery_rows,
)
from draftassist.learning.pool_proposal import Game, Mastery, proposal
from draftassist.learning.profile import Game as ProfileGame
from draftassist.learning.profile import also_plays, rank_summary

log = logging.getLogger(__name__)
KEEP_JOBS = 8  # finished jobs kept for the sheet to read; older ones are dropped
YIELD_EVERY = 10  # matches between reminders to a running crawl


def canonical_riot_id(account: Mapping, typed: str) -> str:
    """The Riot ID as the account spells it (account-v1 answers any letter case)."""
    name, _, tag = typed.partition("#")
    return f"{account.get('gameName') or name}#{account.get('tagLine') or tag}"


def lookup(riot, riot_id: str) -> dict:
    """Step 1: is there such an account, and who is it. `{found, riot_id, level, rank}` or `{found: False}`."""
    account = riot.account(riot_id)
    if not account or not account.get("puuid"):
        return {"found": False}
    summoner = riot.summoner(account["puuid"]) or {}
    return {"found": True, "riot_id": canonical_riot_id(account, riot_id), "level": summoner.get("summonerLevel"),
            "rank": rank_summary(riot.ranks(account["puuid"]))}


@dataclass
class OnboardingJob:
    riot_id: str
    role: str
    roles: list[str] = field(default_factory=list)  # the other roles they play: a pool is proposed for each
    status: str = "reading"  # reading | done | error
    games_read: int = 0
    games_total: int = 0
    proposal: list[dict] = field(default_factory=list)
    also_plays: list[dict] = field(default_factory=list)
    games: list[dict] = field(default_factory=list)  # rows shaped like player_games (without puuid and source)
    mastery: list[list[int]] = field(default_factory=list)
    ranks: list[dict] = field(default_factory=list)
    api_puuid: str | None = None
    error: str | None = None
    retry_at: float | None = None  # epoch seconds: when Riot can be asked again after a full rate limit
    stop: bool = False  # the user skipped the rest, or the player was created: stop reading
    started: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"status": self.status, "riot_id": self.riot_id, "games_read": self.games_read, "games_total": self.games_total,
                "proposal": self.proposal, "also_plays": self.also_plays, "rank": rank_summary(self.ranks), "error": self.error,
                "retry_at": self.retry_at, "roles": [self.role, *self.roles]}


class OnboardingRunner:
    """The jobs. `make_riot()` returns a client factory, or None without a key (asked each time, so a key added in
    Settings works at once); `role_shares()` is the crawl's champion -> role -> share table."""

    def __init__(self, make_riot: Callable[[], Callable[[], object] | None],
                 role_shares: Callable[[], Mapping[int, Mapping[str, float]]], yield_file: Path | None = None):
        self.make_riot, self.role_shares, self.yield_file = make_riot, role_shares, yield_file
        self._lock = threading.Lock()
        self._jobs: dict[str, OnboardingJob] = {}

    def start(self, riot_id: str, role: str, roles: list[str] | tuple[str, ...] = ()) -> str:
        """Start reading a player. `roles`: the other roles they play, each gets a proposed pool. Without a key the
        job is done at once with an empty proposal: the pool is built by hand."""
        factory = self.make_riot()
        job = OnboardingJob(riot_id, role, [r for r in dict.fromkeys(roles) if r != role],
                            status="reading" if factory else "done")
        job_id = uuid.uuid4().hex
        with self._lock:
            done = sorted((j for j in self._jobs.items() if j[1].status != "reading"), key=lambda j: j[1].started)
            for old_id, _ in done[:max(0, len(done) - KEEP_JOBS + 1)]:
                del self._jobs[old_id]
            self._jobs[job_id] = job
        if factory:
            threading.Thread(target=self._work, args=(job, factory), name="player-onboarding", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> OnboardingJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def skip(self, job_id: str) -> OnboardingJob | None:
        """Go on with what has been read so far."""
        job = self.get(job_id)
        if job is not None:
            job.stop = True
        return job

    def fetched(self, riot_id: str, role: str) -> OnboardingJob | None:
        """What was read for this player and role (the newest job), finished or not, and stop it reading further."""
        with self._lock:
            mine = [j for j in self._jobs.values() if j.role == role and j.riot_id.lower() == riot_id.lower() and j.api_puuid]
        job = max(mine, key=lambda j: j.started, default=None)
        if job is not None:
            job.stop = True
        return job

    def _propose(self, job: OnboardingJob) -> None:
        now_ms = int(time.time() * 1000)
        games = [Game(g["champ_id"], g["role"], g["win"], g["game_start_ms"]) for g in job.games]
        shares = self.role_shares()
        job.proposal = [row for role in (job.role, *job.roles)
                        for row in proposal(games, [Mastery(*m) for m in job.mastery], role, shares, now_ms)]
        chosen = {job.role, *job.roles}
        job.also_plays = [r for r in also_plays([ProfileGame(g["match_id"], g["champ_id"], g["role"], g["win"],
                                                             g["game_start_ms"]) for g in job.games], job.role)
                          if r["role"] not in chosen]

    def _work(self, job: OnboardingJob, factory: Callable[[], object]) -> None:
        riot = None
        try:
            if self.yield_file is not None:
                request_yield(self.yield_file)
            riot = factory()
            account = riot.account(job.riot_id)
            if not account or not account.get("puuid"):
                raise LookupError("No such account")
            job.riot_id = canonical_riot_id(account, job.riot_id)
            job.api_puuid = account["puuid"]
            # rank and mastery first: two requests, and a skipped read still has them
            job.ranks = riot.ranks(job.api_puuid)
            job.mastery = mastery_rows(riot.mastery_all(job.api_puuid))
            ids = riot.match_ids(job.api_puuid, None, int(time.time() - WINDOW_DAYS * 86_400), count=MAX_MATCHES_PER_PLAYER)
            job.games_total = len(ids)
            for i, match_id in enumerate(ids):
                if job.stop:
                    break
                if self.yield_file is not None and i % YIELD_EVERY == 0:
                    request_yield(self.yield_file)
                match = riot.match(match_id)
                job.games_read += 1
                info = (match or {}).get("info", {})
                if info.get("gameDuration", 0) < MIN_DURATION_S:
                    continue  # remakes don't count
                me = next((p for p in info.get("participants", []) if p.get("puuid") == job.api_puuid), None)
                if me is not None:
                    job.games.append({"match_id": match_id, "champ_id": me["championId"],
                                      "role": POSITIONS.get(me.get("teamPosition") or ""), "win": bool(me["win"]),
                                      "queue_id": info.get("queueId"), "game_start_ms": info.get("gameStartTimestamp", 0),
                                      **game_stats(me, info)})
            self._propose(job)
            job.status = "done"
        except LookupError as e:
            job.status, job.error = "error", str(e)
        except RiotAuthError as e:
            job.status, job.error = "error", str(e)
        except RiotUnavailable as e:  # what was read so far is still proposed; the rest can be read later
            self._propose(job)
            job.status, job.error, job.retry_at = "error", str(e), time.time() + e.retry_after
        except Exception as e:  # network trouble: say so, the pool can still be built by hand
            log.exception("onboarding %s failed", job.role)
            job.status, job.error = "error", f"Couldn't read their games ({type(e).__name__})"
        finally:
            close = getattr(riot, "close", None)
            if close:
                close()
