"""Fill in data the crawler didn't record when older matches were stored (damage to champions, game
length), from the on-disk Riot cache (no API calls, no key needed), then re-aggregate the current patch.
Safe to re-run.

    uv run python -m draftassist.data.backfill
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine, text
from sqlmodel import Session

from draftassist.data.crawler import parse_damage
from draftassist.data.riot import REGION, cache_path, match_path


def _cached_match(cache_dir: Path, match_id: str) -> dict | None:
    path = cache_path(cache_dir / "riot", REGION, match_path(match_id), None)
    return json.loads(path.read_text(encoding="utf-8"))["body"] if path.is_file() else None


def backfill_damage(db: Engine, cache_dir: Path) -> dict[str, int]:
    """Matches with participants missing damage: updated from the cache, or counted as missing."""
    updated = missing = 0
    with Session(db) as s:
        conn = s.connection()
        ids = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT match_id FROM match_participants WHERE dmg_phys IS NULL ORDER BY match_id"))]
        for match_id in ids:
            body = _cached_match(cache_dir, match_id)
            if not body:
                missing += 1
                continue
            rows = [(p["championId"], *parse_damage(p)) for p in body["info"]["participants"]]
            if any(d is None for _, *dmg in rows for d in dmg):
                missing += 1
                continue
            for champ, phys, magic, true in rows:
                conn.execute(text(
                    "UPDATE match_participants SET dmg_phys = :phys, dmg_magic = :magic, dmg_true = :true "
                    "WHERE match_id = :match_id AND champ_id = :champ"),
                    {"phys": phys, "magic": magic, "true": true, "match_id": match_id, "champ": champ})
            updated += 1
        s.commit()
    return {"updated": updated, "missing": missing}


def backfill_durations(db: Engine, cache_dir: Path) -> dict[str, int]:
    """Matches missing game length (docs/comp-style.md): updated from the cache, or counted as missing."""
    updated = missing = 0
    with Session(db) as s:
        conn = s.connection()
        ids = [r[0] for r in conn.execute(text(
            "SELECT match_id FROM crawled_matches WHERE game_duration_s IS NULL ORDER BY match_id"))]
        for match_id in ids:
            body = _cached_match(cache_dir, match_id)
            duration = (body or {}).get("info", {}).get("gameDuration")
            if duration is None:
                missing += 1
                continue
            conn.execute(text("UPDATE crawled_matches SET game_duration_s = :d WHERE match_id = :m"),
                         {"d": duration, "m": match_id})
            updated += 1
        s.commit()
    return {"updated": updated, "missing": missing}


def main() -> int:
    from draftassist.config import load_settings
    from draftassist.data.aggregate import aggregate
    from draftassist.data.crawler import current_patch
    from draftassist.db.session import init_db

    settings = load_settings()
    db = init_db(settings.db_path)
    result = backfill_damage(db, settings.cache_dir)
    print(f"Damage backfilled for {result['updated']} matches; {result['missing']} not in the cache")
    result = backfill_durations(db, settings.cache_dir)
    print(f"Game length backfilled for {result['updated']} matches; {result['missing']} not in the cache")
    patch = current_patch(db)
    totals = aggregate(db, patch)
    print(f"Re-aggregated {patch}: {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
