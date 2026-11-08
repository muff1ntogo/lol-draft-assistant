# Crawl page: live progress and crawl options (8 Nov 2026)

Reported: the progress bars didn't move during a crawl, and the patch couldn't be chosen.

## Why the bars didn't move

The tier table counted **the current patch in the main database** only, and the log printed only at each stats refresh
(every 15 minutes). So the bars stood still whenever the running job wrote somewhere else: a Master+ crawl (its own
database), an older patch, or "Gold leads first", which spends its first hours fetching timelines and stores no new
matches.

## What changed

- **Live progress.** The app starts each job with `DRAFTASSIST_PROGRESS` pointing at `cache/jobs/progress.json`; the
  crawl writes it at most every 2 s (`data/progress.py`): phase (seeding, timelines, crawling, refreshing stats),
  matches stored this run per tier, players read, timelines done of total, database and patch. The page shows it as
  **This run** (with matches per hour) and polls every 2 s while a job runs. The log also prints a line every 100 matches
  and every 100 timelines.
- **The tier table follows the running crawl** (its patch, and the Master+ database for a Master+ crawl), with selects
  to show another patch or database.
- **Crawl options** for Crawl and Gold leads first (`jobs.crawl_options`, validated): patch (the stored patches, or
  type one as players know it, 26.20, or Riot's 16.20), tiers, match ids per player (1–100) and skip timelines. Master+
  takes none (it always crawls its own range).

Tests: the end of `tests/test_jobs.py`.
