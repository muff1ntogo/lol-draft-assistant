"""Crawl CLI: seed, crawl, aggregate, report coverage.

    uv run python -m draftassist.data.crawl --hours 8            # overnight
    uv run python -m draftassist.data.crawl --max-matches 50     # smoke test

At development-key rates (100 requests / 2 min) expect about 3,000 matches an hour, less while
seeding and listing match ids. Ctrl+C stops cleanly: whatever was stored is aggregated.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from sqlmodel import Session, func, select

from draftassist.config import load_settings
from draftassist.data.aggregate import aggregate
from draftassist.data.coverage import coverage, format_report
from draftassist.data.crawler import BRACKETS, Crawler, CrawlStats, current_patch
from draftassist.data.riot import RiotAuthError, RiotClient
from draftassist.db.models import SeedPlayer
from draftassist.db.session import init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl current-patch ranked matches (EUW)")
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--hours", type=float)
    limit.add_argument("--minutes", type=float)
    parser.add_argument("--max-matches", type=int)
    parser.add_argument("--brackets", nargs="+", default=list(BRACKETS), choices=BRACKETS)
    parser.add_argument("--seed-pages", type=int, default=1, help="league-v4 pages per division when seeding")
    parser.add_argument("--reseed", action="store_true", help="fetch ladder seeds even if some exist")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    db = init_db(settings.db_path)
    patch = current_patch(db)
    seconds = (args.hours or 0) * 3600 + (args.minutes or 0) * 60
    deadline = time.time() + seconds if seconds else None
    if deadline is None and args.max_matches is None:
        parser.error("give --hours, --minutes or --max-matches")

    stats = CrawlStats()
    try:
        riot = RiotClient(settings.riot_api_key, settings.cache_dir)
        crawler = Crawler(riot, db, patch, args.brackets)
        with Session(db) as s:
            seeds = s.exec(select(func.count()).select_from(SeedPlayer)).one()
        if args.reseed or seeds == 0:
            print(f"Seeding {', '.join(args.brackets)} from league-v4...")
            print(f"  {crawler.seed(args.seed_pages)} new seed players")
        print(f"Crawling patch {patch}...")
        stats = crawler.run(max_matches=args.max_matches, deadline=deadline)
    except RiotAuthError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; aggregating what was stored.")
    finally:
        totals = aggregate(db, patch)
    print(f"\nStored this run: {stats.stored} | other patch: {stats.skipped_other_patch} | "
          f"invalid/remake: {stats.skipped_invalid} | players: {stats.players_crawled}")
    print(f"Matches per bracket for {patch}: {totals}\n")
    print(format_report(db, patch, coverage(db, patch)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
