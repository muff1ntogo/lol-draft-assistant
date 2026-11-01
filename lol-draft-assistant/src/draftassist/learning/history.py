"""Our players' recent games, for comfort scoring.

Two sources, merged per (player, match): the games we logged ourselves (no API key needed; the
pick's role comes from the client, else the player's pool) and, for linked players, their ranked
match-v5 history of the last 60 days (Solo/Duo, Flex and Ranked 5s). match-v5 wins when both exist.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.crawler import POSITIONS
from draftassist.data.riot import PLATFORM, RiotAuthError, RiotClient
from draftassist.db.models import Draft, DraftAction, Player, PlayerGame, PoolEntry
from draftassist.learning.comfort import WINDOW_DAYS

log = logging.getLogger(__name__)

MIN_DURATION_S = 300  # remakes don't count
MAX_MATCHES_PER_PLAYER = 100


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
        players = {p.role: p.puuid for p in s.exec(select(Player)).all()}
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


def fetch_history(db: Engine, riot: RiotClient, now: float | None = None) -> dict[str, int]:
    """Each linked player's ranked games of the last 60 days from match-v5. Every response is cached,
    so repeated runs only fetch new matches. Returns games added per role."""
    now = now if now is not None else time.time()
    since = int(now - WINDOW_DAYS * 86_400)
    added: dict[str, int] = {}
    with Session(db) as s:
        players = list(s.exec(select(Player)).all())
        for p in players:  # match-v5 needs the API PUUID: look it up from the Riot ID if we only have the client's
            if not p.api_puuid and p.riot_id:
                p.api_puuid = riot.account_puuid(p.riot_id)
                s.add(p)
        s.commit()
        players = [p for p in s.exec(select(Player)).all() if p.api_puuid]
    for p in players:
        try:
            added[p.role] = _fetch_player(db, riot, p, since)
        except RiotAuthError:
            raise
        except Exception:  # one player's trouble doesn't stop the others
            log.exception("match history for %s failed", p.role)
    return added


def _fetch_player(db: Engine, riot: RiotClient, p: Player, since: int) -> int:
    """Store one player's ranked games since `since` under their internal key (pool entries use it)."""
    ids: list[str] = []
    while len(ids) < MAX_MATCHES_PER_PLAYER:
        page = riot.match_ids(p.api_puuid, None, since, count=100, start=len(ids))
        ids.extend(page)
        if len(page) < 100:
            break
    added = 0
    with Session(db) as s:
        for match_id in ids[:MAX_MATCHES_PER_PLAYER]:
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
                                           source="match-v5"))
        s.commit()
    return added
