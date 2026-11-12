"""In-memory player-onboarding jobs (docs/roster-profiles.md, Stage B)."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from draftassist.data.crawler import MIN_DURATION_S, POSITIONS
from draftassist.data.progress import request_yield
from draftassist.learning.history import mastery_rows
from draftassist.learning.pool_proposal import Game, Mastery, proposal
from draftassist.learning.profile import Game as ProfileGame, also_plays


@dataclass
class OnboardingJob:
    riot_id: str
    role: str
    status: str = "reading"
    games_read: int = 0
    games_total: int = 0
    proposal: list[dict] = field(default_factory=list)
    also_plays: list[dict] = field(default_factory=list)
    games: list[Game] = field(default_factory=list)
    mastery: list[list[int]] = field(default_factory=list)
    ranks: list[dict] = field(default_factory=list)
    api_puuid: str | None = None
    error: str | None = None


class OnboardingRunner:
    """Short-lived jobs. Data is persisted only after the user explicitly creates the player."""
    def __init__(self, make_riot: Callable[[], object] | None, role_shares: Callable[[], dict[int, dict[str, float]]],
                 yield_file: Path | None = None):
        self.make_riot, self.role_shares, self.yield_file = make_riot, role_shares, yield_file
        self._lock = threading.Lock()
        self._jobs: dict[str, OnboardingJob] = {}

    @property
    def available(self) -> bool:
        return self.make_riot is not None

    def start(self, riot_id: str, role: str) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = OnboardingJob(riot_id, role, status="done" if self.make_riot is None else "reading")
        if self.make_riot is None:
            return job_id
        threading.Thread(target=self._work, args=(job_id,), name="player-onboarding", daemon=True).start()
        return job_id

    def get(self, job_id: str) -> OnboardingJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def completed(self, riot_id: str, role: str) -> OnboardingJob | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.status == "done" and j.role == role and
                         j.riot_id.lower() == riot_id.lower()), None)

    def _work(self, job_id: str) -> None:
        job = self.get(job_id)
        riot = None
        try:
            if self.yield_file is not None:
                request_yield(self.yield_file)
            riot = self.make_riot()
            account = riot.account(job.riot_id)
            if not account:
                raise ValueError("No such account")
            job.riot_id = f"{account.get('gameName', job.riot_id.split('#')[0])}#{account.get('tagLine', job.riot_id.split('#')[-1])}"
            job.api_puuid = account["puuid"]
            ids = riot.match_ids(job.api_puuid, None, int(time.time() - 60 * 86_400), count=100)
            job.games_total = len(ids)
            for match_id in ids:
                match = riot.match(match_id)
                job.games_read += 1
                info = (match or {}).get("info", {})
                if info.get("gameDuration", 0) < MIN_DURATION_S:
                    continue
                me = next((p for p in info.get("participants", []) if p.get("puuid") == job.api_puuid), None)
                if me:
                    job.games.append(Game(me["championId"], POSITIONS.get(me.get("teamPosition") or ""),
                                          bool(me["win"]), info.get("gameStartTimestamp", 0)))
            job.mastery = mastery_rows(riot.mastery_all(job.api_puuid))
            job.ranks = riot.ranks(job.api_puuid)
            now_ms = int(time.time() * 1000)
            job.proposal = proposal(job.games, (Mastery(*row) for row in job.mastery), job.role, self.role_shares(), now_ms)
            job.also_plays = also_plays([ProfileGame("", g.champ_id, g.role, g.win, g.start_ms) for g in job.games], job.role)
            job.status = "done"
        except Exception as e:
            job.status, job.error = "error", str(e)
        finally:
            close = getattr(riot, "close", None)
            if close:
                close()
