"""Crawl CLI: seed, crawl, aggregate, report coverage.

    uv run python -m draftassist.data.crawl --hours 8            # overnight
    uv run python -m draftassist.data.crawl --max-matches 50     # smoke test
    uv run python -m draftassist.data.crawl --patch 16.21 --ids-per-queue 100 --hours 8   # deepen an older patch
    uv run python -m draftassist.data.crawl --master --ids-per-queue 100 --hours 12   # Matchup tree only (data/master.py)
    uv run python -m draftassist.data.crawl --timelines-refetch --share 1   # the newer timeline stats for older matches

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
from draftassist.data.crawler import (
    LEAGUE_TIERS,
    MASTER_PLUS,
    QUEUES,
    Crawler,
    CrawlStats,
    brackets_in_use,
    current_patch,
)
from draftassist.data.master import FIRST_PATCH, SOLO_DUO, master_path, prepare_master
from draftassist.data.riot import RiotAuthError, RiotClient, sync_api_key
from draftassist.data.tags import sync_tags
from draftassist.data.timelines import backfill_timelines, estimate_refetch, refetch_sample
from draftassist.db.models import CrawledMatch, SeedPlayer
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
    parser.add_argument("--master", action="store_true",
                        help="Master+ Solo/Duo from 16.21 on, into its own database: only the Matchup tree reads it")
    parser.add_argument("--ids-per-queue", type=int, default=20, metavar="N",
                        help="match ids per player and queue, newest first (default 20, Riot's maximum is 100)")
    parser.add_argument("--no-timelines", action="store_true",
                        help="skip the extra request per match for gold leads at 15 minutes")
    parser.add_argument("--timelines-first", action="store_true",
                        help="first fetch gold leads for matches stored without them (resumable)")
    parser.add_argument("--timelines-refetch", action="store_true",
                        help="fetch timelines again for stored matches that lack the newer stats, then stop; no crawling")
    parser.add_argument("--share", type=float, metavar="F",
                        help="with --timelines-refetch: the share of each bracket that should end up covered (default 0.25)")
    parser.add_argument("--aggregate-every", type=float, default=15, metavar="MINUTES",
                        help="rebuild the stats tables this often while crawling (default 15)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.master and (args.patch or args.brackets):
        parser.error("--master always crawls Master+ from its first patch on: leave out --patch and --brackets")
    if args.share is not None and not args.timelines_refetch:
        parser.error("--share goes with --timelines-refetch")
    share = 0.25 if args.share is None else args.share
    if not 0 < share <= 1:
        parser.error("--share must be above 0, up to 1")
    settings = load_settings()
    db = init_db(settings.db_path)
    if args.master:
        db = prepare_master(db, master_path(settings.db_path))
    sync_tags(db)  # labels for the fits: the file is the source of truth, and the app may not have restarted
    window = current_patch(db)  # what Live reads, whichever patch is being crawled
    patch = FIRST_PATCH if args.master else args.patch or window
    patches = sorted({patch, window}, key=lambda p: tuple(int(x) for x in p.split("."))) if args.master else [patch]
    if args.timelines_refetch:  # every patch's leads can change
        with Session(db) as s:
            patches = sorted(set(s.exec(select(CrawledMatch.patch).distinct()).all()),
                             key=lambda p: tuple(int(x) for x in p.split(".")))
    if args.master:
        brackets = (MASTER_PLUS,)
    else:
        brackets = tuple(args.brackets) if args.brackets else brackets_in_use(db)

    def refresh() -> dict[str, int]:
        totals: dict[str, int] = {}
        for p in patches:
            for bracket, n in aggregate(db, p, window=window).items():
                totals[bracket] = totals.get(bracket, 0) + n
        return totals

    seconds = (args.hours or 0) * 3600 + (args.minutes or 0) * 60
    deadline = time.time() + seconds if seconds else None
    if deadline is None and args.max_matches is None and not args.timelines_refetch:
        parser.error("give --hours, --minutes or --max-matches")

    stats = CrawlStats()
    try:
        riot = RiotClient(settings.riot_api_key, settings.cache_dir)
        if sync_api_key(db, settings.riot_api_key):
            print("New Riot API key: ladder seeds from the old key were dropped (PUUIDs are encrypted per app)")

        def timelines_done() -> bool:
            return deadline is not None and time.time() >= deadline

        def timeline_checkpoint(n: int) -> None:
            refresh()
            print(f"{time.strftime('%H:%M')} timelines: {n} matches so far, stats refreshed", flush=True)

        if args.timelines_refetch:
            ids = refetch_sample(db, share)
            cost = estimate_refetch(ids, settings.cache_dir)
            print(f"Refetching {cost['matches']:,} timelines for {share:.0%} of each bracket: {cost['requests']:,} requests, "
                  f"about {cost['hours']:.1f} estimated hours at the current limits", flush=True)
            result = backfill_timelines(db, riot, settings.cache_dir, timelines_done, timeline_checkpoint,
                                        args.aggregate_every * 60, ids=ids)
            print(f"{time.strftime('%H:%M')} timelines: {result['updated']} matches done, {result['left']} left", flush=True)
        else:
            crawler = Crawler(riot, db, patch, brackets, timelines=not args.no_timelines,
                              queues=SOLO_DUO if args.master else QUEUES, newer_patches=args.master)
            with Session(db) as s:
                seeds = s.exec(select(func.count()).select_from(SeedPlayer)).one()
            if args.reseed or seeds == 0:
                print(f"Seeding {', '.join(brackets)} from league-v4...")
                print(f"  {crawler.seed(args.seed_pages)} new seed players")
            print(f"Crawling patch {patch}... (stats refresh every {args.aggregate_every:g} min)")

            def checkpoint(progress: CrawlStats) -> None:
                totals = refresh()
                print(f"{time.strftime('%H:%M')} stats refreshed: {sum(totals.values())} matches "
                      f"({', '.join(f'{b} {n}' for b, n in totals.items())}); this run +{progress.total_stored}",
                      flush=True)

            if args.timelines_first:
                result = backfill_timelines(db, riot, settings.cache_dir, timelines_done, timeline_checkpoint,
                                            args.aggregate_every * 60)
                print(f"{time.strftime('%H:%M')} timelines: {result['updated']} matches done, {result['left']} left",
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
        totals = refresh()
    print(f"\nStored this run: {stats.stored} | other patch: {stats.skipped_other_patch} | "
          f"invalid/remake: {stats.skipped_invalid} | players: {stats.players_crawled} (rejected {stats.players_rejected})")
    print(f"Matches per bracket for {patch}: {totals}\n")
    print(format_report(db, patch, coverage(db, patch, brackets)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
