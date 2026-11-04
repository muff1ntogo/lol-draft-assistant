"""Gold and XP leads at 15 minutes, from match-v5 timelines (docs/matchup-archetypes.md, Stage 4).

Each laner's lead over the enemy in the same position goes on match_participants. Only these numbers are kept: a raw
timeline is about 800 KB against 300 bytes for what we use, so timelines aren't cached on disk. Another minute would mean
fetching them again. Games under 15 minutes have no lead (NULL) and are marked done, so they aren't fetched twice.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Engine, text
from sqlmodel import Session

from draftassist.data.backfill import _cached_match

LEAD_AT_MS = 15 * 60_000


def lane_diffs(timeline: dict | None, match: dict) -> dict[int, tuple[float, float]]:
    """champ -> (gold lead, XP lead) over the enemy in the same position at 15 minutes. Empty if the game ended first
    or the timeline is missing."""
    frames = (timeline or {}).get("info", {}).get("frames") or []
    frame = next((f for f in frames if f.get("timestamp", 0) >= LEAD_AT_MS), None)
    if frame is None:
        return {}
    stats = frame.get("participantFrames") or {}
    by_position: dict[str, dict[int, tuple[int, float, float]]] = {}
    for i, p in enumerate(match["info"]["participants"]):
        row = stats.get(str(p.get("participantId", i + 1)))
        if row is None or not p.get("teamPosition"):
            continue
        by_position.setdefault(p["teamPosition"], {})[p["teamId"]] = (p["championId"], row["totalGold"], row["xp"])
    out = {}
    for sides in by_position.values():
        if len(sides) != 2:
            continue
        (champ_a, gold_a, xp_a), (champ_b, gold_b, xp_b) = sides.values()
        out[champ_a] = (float(gold_a - gold_b), float(xp_a - xp_b))
        out[champ_b] = (float(gold_b - gold_a), float(xp_b - xp_a))
    return out


def store_leads(db: Engine, match_id: str, diffs: dict[int, tuple[float, float]]) -> None:
    with Session(db) as s:
        conn = s.connection()
        for champ, (gold, xp) in diffs.items():
            conn.execute(text("UPDATE match_participants SET lane_gold_15 = :g, lane_xp_15 = :x "
                              "WHERE match_id = :m AND champ_id = :c"), {"g": gold, "x": xp, "m": match_id, "c": champ})
        conn.execute(text("UPDATE crawled_matches SET timeline_done = 1 WHERE match_id = :m"), {"m": match_id})
        s.commit()


def backfill_timelines(db: Engine, riot, cache_dir: Path, done: Callable[[], bool] = lambda: False,
                       checkpoint: Callable[[int], None] | None = None, checkpoint_every_s: float = 900.0,
                       clock: Callable[[], float] = time.time) -> dict[str, int]:
    """Stored matches without leads yet, newest first, until none are left or `done()`. Safe to stop and restart."""
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(text(
            "SELECT match_id FROM crawled_matches WHERE timeline_done = 0 ORDER BY game_start_ms DESC, match_id"))]
    updated = 0
    last = clock()
    for match_id in ids:
        if done():
            break
        body = _cached_match(cache_dir, match_id) or riot.match(match_id)
        store_leads(db, match_id, lane_diffs(riot.timeline(match_id), body) if body else {})
        updated += 1
        if checkpoint is not None and clock() - last >= checkpoint_every_s:
            checkpoint(updated)
            last = clock()
    return {"updated": updated, "left": len(ids) - updated}
