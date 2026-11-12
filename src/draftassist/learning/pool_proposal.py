"""Automatic initial champion-pool proposals (docs/roster-profiles.md, Stage B).

This module is deliberately pure: onboarding supplies recent ranked games, mastery and the
crawl's role shares; the review UI can always show exactly why each row was suggested.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from draftassist.learning.comfort import wr_adj

DAY_MS = 86_400_000
MAIN_GAMES = 15
MAIN_ROLE_SHARE = 0.25
MAIN_SHARE_GAMES = 8
MAIN_WR = 0.45
COMFORTABLE_GAMES = 5
LEARNING_GAMES = 2
RECENT_LEARNING_DAYS = 14
MASTERY_FILL_TO = 5
MASTERY_MIN_POINTS = 50_000
MASTERY_RECENT_DAYS = 180
MASTERY_COMFORTABLE_POINTS = 200_000
MASTERY_ROLE_SHARE = 0.10
MAX_CHAMPS = 10


@dataclass(frozen=True)
class Game:
    champ_id: int
    role: str | None
    win: bool
    start_ms: int


@dataclass(frozen=True)
class Mastery:
    champ_id: int
    points: int
    last_played_ms: int


def proposal(games: Iterable[Game], mastery: Iterable[Mastery], role: str,
             role_shares: Mapping[int, Mapping[str, float]], now_ms: int) -> list[dict]:
    """Return the role's game-led proposal followed by eligible mastery fill rows."""
    by_champ: dict[int, list[Game]] = defaultdict(list)
    role_games = [g for g in games if g.role == role]
    for game in role_games:
        by_champ[game.champ_id].append(game)
    total = len(role_games)
    rows = []
    for champ_id, played in by_champ.items():
        count, wins = len(played), sum(g.win for g in played)
        recent_one = any(g.start_ms >= now_ms - RECENT_LEARNING_DAYS * DAY_MS for g in played)
        is_main = (count >= MAIN_GAMES or (count >= MAIN_SHARE_GAMES and count / total >= MAIN_ROLE_SHARE)) and \
            wr_adj("comfortable", count, wins) >= MAIN_WR
        tier = "main" if is_main else "comfortable" if count >= COMFORTABLE_GAMES else "learning" if (
            count >= LEARNING_GAMES or recent_one) else None
        if tier:
            rows.append({"champ_id": champ_id, "role": role, "tier": tier, "source": "games",
                         "games": count, "wins": wins, "mastery": 0})
    order = {"main": 0, "comfortable": 1, "learning": 2}
    rows.sort(key=lambda row: (order[row["tier"]], -row["games"], row["champ_id"]))
    seen = {row["champ_id"] for row in rows}
    if len(rows) < MASTERY_FILL_TO:
        cutoff = now_ms - MASTERY_RECENT_DAYS * DAY_MS
        for item in sorted(mastery, key=lambda m: (-m.points, m.champ_id)):
            if len(rows) >= MASTERY_FILL_TO or len(rows) >= MAX_CHAMPS:
                break
            if item.champ_id in seen or item.points < MASTERY_MIN_POINTS or item.last_played_ms < cutoff:
                continue
            if role_shares.get(item.champ_id, {}).get(role, 0.0) < MASTERY_ROLE_SHARE:
                continue
            rows.append({"champ_id": item.champ_id, "role": role,
                         "tier": "comfortable" if item.points >= MASTERY_COMFORTABLE_POINTS else "learning",
                         "source": "mastery", "games": 0, "wins": 0, "mastery": item.points})
            seen.add(item.champ_id)
    return rows[:MAX_CHAMPS]
