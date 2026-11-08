"""Storing what a match-v5 timeline gives (docs/matchup-archetypes.md, Stage 4; docs/draft-vs-execution.md, Stage 1; what's
read and how: data/timeline_map.py).

Only the derived numbers are kept: a raw timeline is about 800 KB and holds PUUIDs and names, so timelines aren't cached on
disk. Another stat means fetching them again. Games under 15 minutes have no numbers and are marked done, so they aren't
fetched twice. `timeline_version` tells a match with everything apart from one with only the older gold and XP leads.
"""
from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from sqlalchemy import Engine, text
from sqlmodel import Session

from draftassist.data.backfill import _cached_match
from draftassist.data.riot import REGION, cache_path, match_path
from draftassist.data.timeline_map import PARTICIPANT_COLUMNS, TimelineStats, parse_timeline
from draftassist.db.models import MatchTeamState

TIMELINE_VERSION = 2  # 1 was gold and XP at 15 only, and was never written: those matches are at 0
REQUESTS_PER_HOUR = 3_000  # the key's 100 requests per 2 minutes


def store_team_state(s: Session, match_id: str, stats: TimelineStats) -> None:
    for team, values in stats.teams.items():
        s.merge(MatchTeamState(match_id=match_id, team_id=team, **values))


def store_timeline(db: Engine, match_id: str, timeline: dict | None, match: dict | None) -> None:
    """One crawled match. A missing timeline is done, so a crawl moves on, but stays at version 0, so a refetch can try it."""
    stats = parse_timeline(timeline, match) if timeline and match else None
    with Session(db) as s:
        conn = s.connection()
        if stats is not None:
            sets = ", ".join(f"{c} = :{c}" for c in PARTICIPANT_COLUMNS)
            for champ, values in stats.participants.items():
                conn.execute(text(f"UPDATE match_participants SET {sets} WHERE match_id = :m AND champ_id = :c"),
                             {**values, "m": match_id, "c": champ})
            store_team_state(s, match_id, stats)
        version = TIMELINE_VERSION if timeline and match else 0
        conn.execute(text("UPDATE crawled_matches SET timeline_done = 1, timeline_version = :v WHERE match_id = :m"),
                     {"v": version, "m": match_id})
        s.commit()


def backfill_timelines(db: Engine, riot, cache_dir: Path, done: Callable[[], bool] = lambda: False,
                       checkpoint: Callable[[int], None] | None = None, checkpoint_every_s: float = 900.0,
                       clock: Callable[[], float] = time.time, ids: Iterable[str] | None = None,
                       on_match: Callable[[int, int], None] | None = None) -> dict[str, int]:
    """`ids`, or stored matches with no timeline yet, newest first, until none are left or `done()`. Safe to stop and restart."""
    if ids is None:
        with db.connect() as conn:
            ids = [r[0] for r in conn.execute(text(
                "SELECT match_id FROM crawled_matches WHERE timeline_done = 0 ORDER BY game_start_ms DESC, match_id"))]
    ids = list(ids)
    updated = 0
    last = clock()
    for match_id in ids:
        if done():
            break
        body = _cached_match(cache_dir, match_id) or riot.match(match_id)
        store_timeline(db, match_id, riot.timeline(match_id) if body else None, body)
        updated += 1
        if on_match is not None:  # (done, total): the Crawl page's live progress
            on_match(updated, len(ids))
        if checkpoint is not None and clock() - last >= checkpoint_every_s:
            checkpoint(updated)
            last = clock()
    return {"updated": updated, "left": len(ids) - updated}


def refetch_sample(db: Engine, share: float) -> list[str]:
    """Matches to fetch so that `share` of each bracket has everything. The same order every time, and what's already done
    counts, so stopping and running it again carries on rather than taking another share."""
    with db.connect() as conn:
        rows = conn.execute(text("SELECT bracket, match_id, timeline_version FROM crawled_matches ORDER BY match_id")).all()
    by_bracket: dict[str, list] = {}
    for bracket, match_id, version in rows:
        by_bracket.setdefault(bracket, []).append((match_id, version))
    sample = []
    for bracket in sorted(by_bracket):
        matches = by_bracket[bracket]
        missing = [m for m, version in matches if version < TIMELINE_VERSION]
        random.Random(bracket).shuffle(missing)
        have = len(matches) - len(missing)
        sample += missing[:max(0, math.ceil(share * len(matches)) - have)]
    return sample


def estimate_refetch(ids: list[str], cache_dir: Path) -> dict[str, float]:
    """One request per timeline, and one per match not in the disk cache."""
    uncached = sum(not cache_path(cache_dir / "riot", REGION, match_path(m), None).is_file() for m in ids)
    requests = len(ids) + uncached
    return {"matches": len(ids), "requests": requests, "hours": requests / REQUESTS_PER_HOUR}
