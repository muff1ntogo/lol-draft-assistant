"""Riot API client: one shared token-bucket rate limiter and an on-disk cache of every response.

The API key travels only in the X-Riot-Token header; it is never logged, printed or cached.

Riot encrypts PUUIDs per app, so PUUIDs fetched with one key are rejected (400) by another key's app.
Cache entries carry a short one-way fingerprint of the key (`key_id`) and an entry from another key is a
miss; `sync_api_key` drops the stored PUUIDs (ladder seeds, our players' API PUUIDs) when the key changes.
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


def key_id(api_key: str) -> str:
    """Short one-way fingerprint telling keys apart without storing them."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


API_KEY_SETTING = "riot_api_key_id"


def sync_api_key(db, api_key: str) -> bool:
    """Record the key in use. On a change (or the first time, when the origin of stored PUUIDs is unknown)
    drop what was fetched under the old key: ladder seeds and our players' API PUUIDs (the learning job
    re-resolves those from Riot IDs). Returns True if anything was reset."""
    from sqlmodel import Session, delete, select

    from draftassist.db.models import AppSetting, Player, SeedPlayer

    current = key_id(api_key)
    with Session(db) as s:
        row = s.get(AppSetting, API_KEY_SETTING)
        if row is not None and json.loads(row.value) == current:
            return False
        s.exec(delete(SeedPlayer))
        for p in s.exec(select(Player)).all():
            if p.api_puuid:
                p.api_puuid = None
                s.add(p)
        row = row or AppSetting(key=API_KEY_SETTING, value="")
        row.value = json.dumps(current)
        s.add(row)
        s.commit()
    log.info("Riot API key changed: ladder seeds and API PUUIDs reset")
    return True


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


def cache_path(cache_root: Path, host: str, path: str, params: dict | None) -> Path:
    """Where RiotClient caches GET {host}{path}?{params}; cache_root is `<cache_dir>/riot`."""
    key = json.dumps([host, path, sorted((params or {}).items())])
    return cache_root / host / f"{hashlib.sha1(key.encode()).hexdigest()}.json"


def match_path(match_id: str) -> str:
    return f"/lol/match/v5/matches/{match_id}"


class RiotClient:
    def __init__(self, api_key: str, cache_dir: Path, limiter: RateLimiter | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, max_retries: int = 5):
        if not api_key:
            raise RiotAuthError("RIOT_API_KEY is not set (put it in .env)")
        self._http = httpx.Client(timeout=30, transport=transport, headers={"X-Riot-Token": api_key})
        self._cache = cache_dir / "riot"
        self.key_id = key_id(api_key)
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self._sleep, self._clock, self._max_retries = sleep, clock, max_retries
        self.requests_made = 0

    def __repr__(self) -> str:
        return f"RiotClient(cache={self._cache}, requests={self.requests_made})"

    def close(self) -> None:
        self._http.close()

    # ---------- cache ----------
    def _cache_path(self, host: str, path: str, params: dict | None) -> Path:
        return cache_path(self._cache, host, path, params)

    def get(self, host: str, path: str, params: dict | None = None, ttl: float | None = None,
            cache_body: bool = True) -> Any:
        """GET https://{host}.api.riotgames.com{path}. Returns parsed JSON, or None for 404.
        Cached responses are reused forever (ttl=None) or while younger than ttl seconds, if fetched
        with the same key (bodies hold PUUIDs, which are encrypted per app). `cache_body=False`: neither read nor
        written, for responses too large to keep."""
        cache = self._cache_path(host, path, params)
        if cache_body and cache.is_file():
            entry = json.loads(cache.read_text(encoding="utf-8"))
            if entry.get("key") == self.key_id and (ttl is None or self._clock() - entry["fetched_at"] < ttl):
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
                                    "Development keys expire after 24 hours: generate a new one (or use "
                                    "your app's key) at https://developer.riotgames.com and update "
                                    "RIOT_API_KEY in .env")
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
            if not cache_body:
                return body
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"fetched_at": self._clock(), "key": self.key_id, "body": body}),
                             encoding="utf-8")
            return body
        raise RuntimeError(f"Riot API: giving up on {path} after {self._max_retries + 1} attempts")

    # ---------- endpoints ----------
    def league_entries(self, tier: str, division: str, page: int = 1) -> list[dict]:
        path = f"/lol/league/v4/entries/RANKED_SOLO_5x5/{tier}/{division}"
        return self.get(PLATFORM, path, {"page": page}, ttl=7 * DAY) or []

    def apex_league(self, tier: str) -> dict:
        """One whole apex ladder ("challenger", "grandmaster", "master"), which has no divisions or pages."""
        return self.get(PLATFORM, f"/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5", ttl=7 * DAY) or {}

    def match_ids(self, puuid: str, queue: int | None, start_time: int, count: int = 20, start: int = 0,
                  end_time: int | None = None) -> list[str]:
        """Ranked match ids since start_time (and before end_time); queue None = every ranked queue (Solo/Duo, Flex,
        Ranked 5s)."""
        path = f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        params = {"type": "ranked", "startTime": start_time, "count": count}
        if queue is not None:
            params["queue"] = queue
        if start:
            params["start"] = start
        if end_time is not None:
            params["endTime"] = end_time
        return self.get(REGION, path, params, ttl=0.25 * DAY) or []

    def match(self, match_id: str) -> dict | None:
        return self.get(REGION, match_path(match_id))

    def timeline(self, match_id: str) -> dict | None:
        """The minute-by-minute frames of a match, about 800 KB each: not cached (data/timelines.py)."""
        return self.get(REGION, match_path(match_id) + "/timeline", cache_body=False)

    def account_puuid(self, riot_id: str) -> str | None:
        """'Name#TAG' -> PUUID (account-v1), None if no such account."""
        from urllib.parse import quote

        name, _, tag = riot_id.partition("#")
        body = self.get(REGION, f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}")
        return body.get("puuid") if body else None
