"""Riot API client: one shared token-bucket rate limiter and an on-disk cache of every response.

The API key travels only in the X-Riot-Token header; it is never logged, printed or cached.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

PLATFORM = "euw1"  # league-v4, summoner, mastery
REGION = "europe"  # match-v5, account-v1
DEV_KEY_LIMITS = ((20, 1.0), (100, 120.0))  # development key: 20 / 1 s and 100 / 2 min

DAY = 86_400.0


class RiotAuthError(RuntimeError):
    """The key was rejected (401/403). Development keys expire after 24 hours."""


class RateLimiter:
    """Sliding-window limiter over several windows at once, shared by every request.
    Windows follow the server's X-App-Rate-Limit header when it is sent."""

    def __init__(self, limits=DEV_KEY_LIMITS, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, margin: float = 0.05):
        self._clock, self._sleep, self._margin = clock, sleep, margin
        self._lock = threading.Lock()
        self._set(limits)

    def _set(self, limits) -> None:
        self.limits = tuple(limits)
        self._hits = [deque() for _ in self.limits]

    def update_from_header(self, header: str | None) -> None:
        """Header format: "20:1,100:120" (count:seconds)."""
        if not header:
            return
        try:
            limits = tuple(sorted((int(c), float(s)) for c, s in (p.split(":") for p in header.split(","))))
        except ValueError:
            return
        with self._lock:
            if limits != self.limits:
                self._set(limits)

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                wait = 0.0
                for (count, window), hits in zip(self.limits, self._hits):
                    while hits and now - hits[0] >= window:
                        hits.popleft()
                    if len(hits) >= count:
                        wait = max(wait, window - (now - hits[0]) + self._margin)
                if wait <= 0:
                    for hits in self._hits:
                        hits.append(now)
                    return
            self._sleep(wait)


class RiotClient:
    def __init__(self, api_key: str, cache_dir: Path, limiter: RateLimiter | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, max_retries: int = 5):
        if not api_key:
            raise RiotAuthError("RIOT_API_KEY is not set (put it in .env)")
        self._http = httpx.Client(timeout=30, transport=transport, headers={"X-Riot-Token": api_key})
        self._cache = cache_dir / "riot"
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self._sleep, self._clock, self._max_retries = sleep, clock, max_retries
        self.requests_made = 0
        self.cache_hits = 0

    def __repr__(self) -> str:
        return f"RiotClient(cache={self._cache}, requests={self.requests_made})"

    def close(self) -> None:
        self._http.close()

    # ---------- cache ----------
    def _cache_path(self, host: str, path: str, params: dict | None) -> Path:
        key = json.dumps([host, path, sorted((params or {}).items())])
        return self._cache / host / f"{hashlib.sha1(key.encode()).hexdigest()}.json"

    def get(self, host: str, path: str, params: dict | None = None, ttl: float | None = None) -> Any:
        """GET https://{host}.api.riotgames.com{path}. Returns parsed JSON, or None for 404.
        Cached responses are reused forever (ttl=None) or while younger than ttl seconds."""
        cache = self._cache_path(host, path, params)
        if cache.is_file():
            entry = json.loads(cache.read_text(encoding="utf-8"))
            if ttl is None or self._clock() - entry["fetched_at"] < ttl:
                self.cache_hits += 1
                return entry["body"]

        url = f"https://{host}.api.riotgames.com{path}"
        for attempt in range(self._max_retries + 1):
            self.limiter.acquire()
            self.requests_made += 1
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as e:
                log.warning("network error on %s (%s); retrying", path, type(e).__name__)
                self._sleep(min(2 ** attempt, 30))
                continue
            self.limiter.update_from_header(resp.headers.get("X-App-Rate-Limit"))
            if resp.status_code in (401, 403):
                raise RiotAuthError(f"Riot API rejected the key ({resp.status_code}). "
                                    "Development keys expire after 24 hours: generate a new one at "
                                    "https://developer.riotgames.com and update RIOT_API_KEY in .env")
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.info("rate limited on %s; waiting %.0f s", path, wait)
                self._sleep(wait)
                continue
            if resp.status_code >= 500:
                self._sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code == 404:
                body = None
            else:
                resp.raise_for_status()
                body = resp.json()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"fetched_at": self._clock(), "body": body}), encoding="utf-8")
            return body
        raise RuntimeError(f"Riot API: giving up on {path} after {self._max_retries + 1} attempts")

    # ---------- endpoints ----------
    def league_entries(self, tier: str, division: str, page: int = 1) -> list[dict]:
        path = f"/lol/league/v4/entries/RANKED_SOLO_5x5/{tier}/{division}"
        return self.get(PLATFORM, path, {"page": page}, ttl=7 * DAY) or []

    def match_ids(self, puuid: str, queue: int, start_time: int, count: int = 20) -> list[str]:
        path = f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        params = {"queue": queue, "type": "ranked", "startTime": start_time, "count": count}
        return self.get(REGION, path, params, ttl=0.25 * DAY) or []

    def match(self, match_id: str) -> dict | None:
        return self.get(REGION, f"/lol/match/v5/matches/{match_id}")

    def account_puuid(self, riot_id: str) -> str | None:
        """'Name#TAG' -> PUUID (account-v1), None if no such account."""
        from urllib.parse import quote

        name, _, tag = riot_id.partition("#")
        body = self.get(REGION, f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}")
        return body.get("puuid") if body else None
