"""Crawl CLI: seed, crawl, aggregate, report coverage.

    uv run python -m draftassist.data.crawl --hours 8            # overnight
    uv run python -m draftassist.data.crawl --max-matches 50     # smoke test
    uv run python -m draftassist.data.crawl --patch 16.21 --ids-per-queue 100 --hours 8   # deepen an older patch

Stats are re-aggregated every 15 minutes while it runs (--aggregate-every), so a running app picks
them up between drafts.

At development or personal-key rates (100 requests / 2 min) expect about 1,800 matches an hour, less while
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
from draftassist.data.crawler import LEAGUE_TIERS, Crawler, CrawlStats, brackets_in_use, current_patch
from draftassist.data.riot import RiotAuthError, RiotClient, sync_api_key
from draftassist.data.tags import sync_tags
from draftassist.data.timelines import backfill_timelines
from draftassist.db.models import SeedPlayer
from draftassist.db.session import init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl current-patch ranked matches (EUW)")
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--hours", type=float)
    limit.add_argument("--minutes", type=float)
    parser.add_argument("--max-matches", type=int)
    parser.add_argument("--brackets", nargs="+", choices=LEAGUE_TIERS,
                        help="default: every tier up to Emerald, plus any bracket a player is set to")
    parser.add_argument("--seed-pages", type=int, default=1, help="league-v4 pages per division when seeding")
    parser.add_argument("--reseed", action="store_true", help="fetch ladder seeds even if some exist")
    parser.add_argument("--patch", help="crawl this older patch instead of the current one, e.g. 16.21")
    parser.add_argument("--ids-per-queue", type=int, default=20, metavar="N",
                        help="match ids per player and queue, newest first (default 20, Riot's maximum is 100)")
    parser.add_argument("--no-timelines", action="store_true",
                        help="skip the extra request per match for gold leads at 15 minutes")
    parser.add_argument("--timelines-first", action="store_true",
                        help="first fetch gold leads for matches stored without them (resumable)")
    parser.add_argument("--aggregate-every", type=float, default=15, metavar="MINUTES",
                        help="rebuild the stats tables this often while crawling (default 15)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = load_settings()
    db = init_db(settings.db_path)
    sync_tags(db)  # labels for the fits: the file is the source of truth, and the app may not have restarted
    window = current_patch(db)  # what Live reads, whichever patch is being crawled
    patch = args.patch or window
    brackets = tuple(args.brackets) if args.brackets else brackets_in_use(db)
    seconds = (args.hours or 0) * 3600 + (args.minutes or 0) * 60
    deadline = time.time() + seconds if seconds else None
    if deadline is None and args.max_matches is None:
        parser.error("give --hours, --minutes or --max-matches")

    stats = CrawlStats()
    try:
        riot = RiotClient(settings.riot_api_key, settings.cache_dir)
        if sync_api_key(db, settings.riot_api_key):
            print("New Riot API key: ladder seeds from the old key were dropped (PUUIDs are encrypted per app)")
        crawler = Crawler(riot, db, patch, brackets, timelines=not args.no_timelines)
        with Session(db) as s:
            seeds = s.exec(select(func.count()).select_from(SeedPlayer)).one()
        if args.reseed or seeds == 0:
            print(f"Seeding {', '.join(brackets)} from league-v4...")
            print(f"  {crawler.seed(args.seed_pages)} new seed players")
        print(f"Crawling patch {patch}... (stats refresh every {args.aggregate_every:g} min)")

        def checkpoint(progress: CrawlStats) -> None:
            totals = aggregate(db, patch, window=window)
            print(f"{time.strftime('%H:%M')} stats refreshed: {sum(totals.values())} matches "
                  f"({', '.join(f'{b} {n}' for b, n in totals.items())}); this run +{progress.total_stored}",
                  flush=True)

        if args.timelines_first:
            def timelines_done() -> bool:
                return deadline is not None and time.time() >= deadline

            def timeline_checkpoint(n: int) -> None:
                aggregate(db, patch, window=window)
                print(f"{time.strftime('%H:%M')} gold leads: {n} matches so far, stats refreshed", flush=True)

            result = backfill_timelines(db, riot, settings.cache_dir, timelines_done, timeline_checkpoint,
                                        args.aggregate_every * 60)
            print(f"{time.strftime('%H:%M')} gold leads: {result['updated']} matches done, {result['left']} left",
                  flush=True)
        stats = crawler.run(max_matches=args.max_matches, deadline=deadline, ids_per_queue=args.ids_per_queue,
                            checkpoint=checkpoint,
                            checkpoint_every_s=args.aggregate_every * 60)
    except RiotAuthError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted; aggregating what was stored.")
    finally:
        totals = aggregate(db, patch, window=window)
    print(f"\nStored this run: {stats.stored} | other patch: {stats.skipped_other_patch} | "
          f"invalid/remake: {stats.skipped_invalid} | players: {stats.players_crawled} (rejected {stats.players_rejected})")
    print(f"Matches per bracket for {patch}: {totals}\n")
    print(format_report(db, patch, coverage(db, patch, brackets)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
