"""Our players' recent games, for comfort scoring.

Two sources, merged per (player, match): the games we logged ourselves (no API key needed; the
pick's role comes from the client, else the player's pool) and, for linked players, their ranked
match-v5 history of the last 60 days (Solo/Duo, Flex and Ranked 5s). match-v5 wins when both exist.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.crawler import POSITIONS
from draftassist.data.riot import PLATFORM, RiotAuthError, RiotClient, RiotUnavailable
from draftassist.db.models import Draft, DraftAction, Player, PlayerGame, PoolEntry
from draftassist.learning.comfort import WINDOW_DAYS

log = logging.getLogger(__name__)

MIN_DURATION_S = 300  # remakes don't count
MAX_MATCHES_PER_PLAYER = 100
PROFILE_TTL_S = 86_400  # rank and mastery are read again at most once a day


def game_stats(me: dict, info: dict) -> dict:
    """What a player did in a match, as the player_games columns (docs/roster-profiles.md)."""
    return {"kills": me.get("kills"), "deaths": me.get("deaths"), "assists": me.get("assists"),
            "cs": (me.get("totalMinionsKilled") or 0) + (me.get("neutralMinionsKilled") or 0)
                  if me.get("totalMinionsKilled") is not None else None,
            "vision": me.get("visionScore"), "duration_s": info.get("gameDuration")}


def _upsert(s: Session, game: PlayerGame) -> bool:
    existing = s.get(PlayerGame, (game.puuid, game.match_id))
    if existing is not None and (existing.source == "match-v5" or game.source == "logged"):
        return False  # match-v5 is the record of truth
    if existing is not None:
        s.delete(existing)
        s.flush()
    s.add(game)
    return True


def collect_logged(db: Engine) -> int:
    """Our picks in logged games with a result -> player games."""
    added = 0
    with Session(db) as s:
        players = {p.role: p.puuid for p in s.exec(select(Player)).all() if p.active}  # who played each role
        pools: dict[int, list[str]] = {}
        for e in s.exec(select(PoolEntry)).all():
            pools.setdefault(e.champ_id, []).append(e.role)
        for d in s.exec(select(Draft).where(Draft.result.in_(("win", "loss")))).all():
            match_id = f"{PLATFORM.upper()}_{d.game_id}" if d.game_id else f"draft-{d.draft_id}"
            picks = s.exec(select(DraftAction).where(DraftAction.draft_id == d.draft_id, DraftAction.type == "pick",
                                                     DraftAction.side == d.side)).all()
            for a in picks:
                role = a.role or (pools[a.champ_id][0] if len(pools.get(a.champ_id, [])) == 1 else None)
                puuid = players.get(role) if role else None
                if puuid is None:
                    continue
                start_ms = int(d.played_at.timestamp() * 1000)
                added += _upsert(s, PlayerGame(puuid=puuid, match_id=match_id, champ_id=a.champ_id, role=role,
                                               win=d.result == "win", queue_id=d.queue_id, game_start_ms=start_ms,
                                               source="logged"))
        s.commit()
    return added


Progress = Callable[[dict], None]


def fetch_history(db: Engine, riot: RiotClient, now: float | None = None, progress: Progress | None = None,
                  failed: list[str] | None = None) -> dict[str, int]:
    """Each linked player's ranked games of the last 60 days from match-v5. Every response is cached,
    so repeated runs only fetch new matches. Returns games added per role.
    `progress` hears {player, players_done, players, games_read, games} as it goes; `failed` collects the Riot IDs
    whose history couldn't be read (the others still are). RiotAuthError and RiotUnavailable (a full rate limit) stop
    the whole fetch: the next player would only fail the same way."""
    now = now if now is not None else time.time()
    since = int(now - WINDOW_DAYS * 86_400)
    added: dict[str, int] = {}
    with Session(db) as s:
        players = [p for p in s.exec(select(Player)).all() if p.active]  # the bench costs no requests
        for p in players:  # match-v5 needs the API PUUID: look it up from the Riot ID if we only have the client's
            if not p.api_puuid and p.riot_id:
                p.api_puuid = riot.account_puuid(p.riot_id)
                s.add(p)
        s.commit()
        players = [p for p in s.exec(select(Player)).all() if p.api_puuid and p.active]
    for i, p in enumerate(players):
        def tell(read: int, total: int, p=p, i=i) -> None:
            if progress is not None:
                progress({"player": p.riot_id, "players_done": i, "players": len(players), "games_read": read,
                          "games": total})
        tell(0, 0)
        try:
            try:
                added[p.role] = _fetch_player(db, riot, p, since, tell)
            except httpx.HTTPStatusError as e:
                # 400: a PUUID from another Riot API key (encrypted per app). Look it up again, retry once.
                if e.response.status_code != 400 or not p.riot_id or not (fresh := _re_resolve(db, riot, p)):
                    raise
                added[p.role] = _fetch_player(db, riot, fresh, since, tell)
        except (RiotAuthError, RiotUnavailable):
            raise
        except Exception:  # one player's trouble doesn't stop the others
            log.exception("match history for %s failed", p.role)
            if failed is not None:
                failed.append(p.riot_id or p.role)
    return added


def _re_resolve(db: Engine, riot: RiotClient, p: Player) -> Player | None:
    puuid = riot.account_puuid(p.riot_id)
    if not puuid or puuid == p.api_puuid:
        return None
    log.warning("%s's API PUUID was rejected; re-resolved it from their Riot ID", p.role)
    with Session(db) as s:
        stored = s.get(Player, p.puuid)
        stored.api_puuid = puuid
        s.add(stored)
        s.commit()
        s.refresh(stored)
        s.expunge(stored)
    return stored


def _fetch_player(db: Engine, riot: RiotClient, p: Player, since: int,
                  tell: Callable[[int, int], None] = lambda read, total: None) -> int:
    """Store one player's ranked games since `since` under their internal key (pool entries use it)."""
    ids: list[str] = []
    while len(ids) < MAX_MATCHES_PER_PLAYER:
        page = riot.match_ids(p.api_puuid, None, since, count=100, start=len(ids))
        ids.extend(page)
        if len(page) < 100:
            break
    added = 0
    ids = ids[:MAX_MATCHES_PER_PLAYER]
    tell(0, len(ids))
    with Session(db) as s:
        for n, match_id in enumerate(ids, 1):
            if n % 5 == 0:
                tell(n, len(ids))
            existing = s.get(PlayerGame, (p.puuid, match_id))
            if existing is not None and existing.source == "match-v5":
                continue
            match = riot.match(match_id)
            if not match or match["info"].get("gameDuration", 0) < MIN_DURATION_S:
                continue
            info = match["info"]
            me = next((x for x in info["participants"] if x.get("puuid") == p.api_puuid), None)
            if me is None:
                continue
            added += _upsert(s, PlayerGame(puuid=p.puuid, match_id=match_id, champ_id=me["championId"],
                                           role=POSITIONS.get(me.get("teamPosition") or ""), win=bool(me["win"]),
                                           queue_id=info.get("queueId"), game_start_ms=info.get("gameStartTimestamp", 0),
                                           source="match-v5", **game_stats(me, info)))
            s.commit()  # match by match, so a run stopped by Riot keeps what it read
        s.commit()
    tell(len(ids), len(ids))
    return added


def backfill_game_stats(db: Engine, riot: RiotClient) -> int:
    """KDA, CS, vision and length for games stored before those columns existed, from matches already in the Riot
    cache: no API calls. Games whose match isn't cached (or was never a match) stay without them. Returns rows filled."""
    filled = 0
    with Session(db) as s:
        api = {p.puuid: p.api_puuid for p in s.exec(select(Player)).all() if p.api_puuid}
        rows = s.exec(select(PlayerGame).where(PlayerGame.kills.is_(None))).all()  # type: ignore[union-attr]
        for g in rows:
            if g.puuid not in api or g.match_id.startswith("draft-"):
                continue
            match = riot.match_cached(g.match_id)
            me = next((x for x in (match or {}).get("info", {}).get("participants", []) if x.get("puuid") == api[g.puuid]), None)
            if me is None:
                continue
            for k, v in game_stats(me, match["info"]).items():
                setattr(g, k, v)
            s.add(g)
            filled += 1
        s.commit()
    return filled


def _epoch(dt: datetime | None) -> float | None:
    """SQLite hands datetimes back without their zone: ours are stored as UTC."""
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def mastery_rows(masteries: list[dict]) -> list[list[int]]:
    """champion-mastery-v4 -> [[champ_id, points, last_played_ms], ...], highest first."""
    rows = [[m["championId"], m.get("championPoints", 0), m.get("lastPlayTime", 0)] for m in masteries if m.get("championId")]
    return sorted(rows, key=lambda r: -r[1])


def refresh_profiles(db: Engine, riot: RiotClient, now: float | None = None, only: str | None = None) -> int:
    """Rank and mastery for every linked player (the bench too: two requests each), at most once a day.
    `only`: just this player's key, whatever its age (a player being added). Returns players refreshed."""
    now = now if now is not None else time.time()
    done = 0
    with Session(db) as s:
        for p in s.exec(select(Player)).all():
            if not p.api_puuid or (only is not None and p.puuid != only):
                continue
            fetched = _epoch(p.profile_fetched_at)
            if only is None and fetched is not None and now - fetched < PROFILE_TTL_S:
                continue
            try:
                p.rank_json = json.dumps(riot.ranks(p.api_puuid))
                p.mastery_json = json.dumps(mastery_rows(riot.mastery_all(p.api_puuid)))
            except (RiotAuthError, RiotUnavailable):
                raise
            except Exception:  # one player's trouble doesn't stop the others
                log.exception("profile for %s failed", p.role)
                continue
            p.profile_fetched_at = datetime.fromtimestamp(now, UTC)
            s.add(p)
            done += 1
        s.commit()
    return done
