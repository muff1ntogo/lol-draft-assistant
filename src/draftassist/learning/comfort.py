"""Comfort scoring (spec, Data model: Comfort scoring, v2).

For every pool champion, from the player's games with it in that role over the last 60 days:

    wr_adj  = (wins + k * prior) / (games + k),   k = 10, prior from the manual tier
              (main 53%, comfortable 50%, learning 46%)
    comfort = 0.6 * (wr_adj - 0.5) * 10 + 0.4 * log(1 + min(recent_games, cap))

The cap (Settings, default 10 games) stops comfort growing without limit with the number of games;
tiers below still count every game.

    main        = 15+ recent games and wr_adj >= 50%
    comfortable = 5+ recent games
    learning    = everything else

The scored tier is a proposal: the player accepts it on the Pools page. A locked tier is never
proposed over. With no games, comfort equals the v1 tier comfort, so the engine is continuous.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.db.models import Player, PlayerGame, PoolEntry
from draftassist.engine.model import TIER_PRIOR_WR

PRIOR_K = 10
DEFAULT_GAMES_CAP = 10  # games beyond this add no more comfort (Settings: comfort_games_cap)
WINDOW_DAYS = 60
MAIN_GAMES = 15
COMFORTABLE_GAMES = 5


def wr_adj(manual_tier: str, games: int, wins: int) -> float:
    prior = TIER_PRIOR_WR.get(manual_tier, 0.5)
    return (wins + PRIOR_K * prior) / (games + PRIOR_K)


def comfort_score(manual_tier: str, games: int, wins: int, games_cap: int | None = DEFAULT_GAMES_CAP) -> float:
    counted = games if not games_cap else min(games, games_cap)
    return 0.6 * (wr_adj(manual_tier, games, wins) - 0.5) * 10 + 0.4 * math.log(1 + counted)


def configured_cap(db: Engine) -> int | None:
    from draftassist.data.settings_store import get_engine_settings

    return get_engine_settings(db)["comfort_games_cap"]


def scored_tier(manual_tier: str, games: int, wins: int) -> str:
    if games >= MAIN_GAMES and wr_adj(manual_tier, games, wins) >= 0.5:
        return "main"
    if games >= COMFORTABLE_GAMES:
        return "comfortable"
    return "learning"


@dataclass(frozen=True)
class TierChange:
    puuid: str
    role: str
    champ_id: int
    manual_tier: str
    scored_tier: str
    games: int
    wins: int


def rescore(db: Engine, now_ms: int | None = None, games_cap: int | None = None) -> list[TierChange]:
    """Recompute games, wins, last played, comfort and scored tier for every pool entry from the
    player's recent games. Returns the open proposals (scored tier differs, not locked)."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    since = now_ms - WINDOW_DAYS * 86_400_000
    cap = games_cap if games_cap is not None else configured_cap(db)
    proposals = []
    with Session(db) as s:
        roles = {p.puuid: p.role for p in s.exec(select(Player)).all()}
        recent: dict[tuple[str, int, str | None], list[PlayerGame]] = {}
        for g in s.exec(select(PlayerGame).where(PlayerGame.game_start_ms >= since)).all():
            recent.setdefault((g.puuid, g.champ_id, g.role), []).append(g)
        for e in s.exec(select(PoolEntry)).all():
            if roles.get(e.puuid) != e.role:
                continue
            games = recent.get((e.puuid, e.champ_id, e.role), [])
            e.games, e.wins = len(games), sum(g.win for g in games)
            e.last_played = (datetime.fromtimestamp(max(g.game_start_ms for g in games) / 1000, UTC)
                             if games else None)
            e.comfort = comfort_score(e.manual_tier, e.games, e.wins, cap)
            e.scored_tier = scored_tier(e.manual_tier, e.games, e.wins)
            s.add(e)
            if e.scored_tier != e.manual_tier and not e.tier_locked:
                proposals.append(TierChange(e.puuid, e.role, e.champ_id, e.manual_tier, e.scored_tier, e.games, e.wins))
        s.commit()
    return proposals


def accept(db: Engine, role: str, champ_id: int | None = None) -> int:
    """Accept scored tiers: one champion, or (champ_id None) every proposal in the role
    ("all" for every role). Locked entries are never changed. Returns how many changed."""
    changed = 0
    cap = configured_cap(db)
    with Session(db) as s:
        roles = {p.puuid: p.role for p in s.exec(select(Player)).all()}
        for e in s.exec(select(PoolEntry)).all():
            if roles.get(e.puuid) != e.role or e.tier_locked or not e.scored_tier:
                continue
            if (role != "all" and e.role != role) or (champ_id is not None and e.champ_id != champ_id):
                continue
            if e.scored_tier != e.manual_tier:
                e.manual_tier = e.scored_tier
                e.comfort = comfort_score(e.manual_tier, e.games, e.wins, cap)
                s.add(e)
                changed += 1
        s.commit()
    return changed


def set_locked(db: Engine, role: str, champ_id: int, locked: bool) -> bool:
    with Session(db) as s:
        roles = {p.puuid: p.role for p in s.exec(select(Player)).all()}
        for e in s.exec(select(PoolEntry).where(PoolEntry.role == role, PoolEntry.champ_id == champ_id)).all():
            if roles.get(e.puuid) == role:
                e.tier_locked = locked
                s.add(e)
                s.commit()
                return True
    return False
