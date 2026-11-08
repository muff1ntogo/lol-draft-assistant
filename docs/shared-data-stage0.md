# Shared data: Stage 0 report and plan (8 Nov 2026)

Nothing in the repo changed. This container has no copy of your data folder, so item 1 (sizes) needs one run of
`measure_storage.py` on your machine (see "What I need from you"). Items 2–4 come from the code, PyPI and turso.tech.

---

## 1. Sizes: run the script

`measure_storage.py` uses only the standard library, opens every database with `mode=ro` and makes no network calls. It
prints no bodies, names, PUUIDs or keys. Safe to run while a crawl is going (WAL).

```powershell
uv run python measure_storage.py                                   # %LOCALAPPDATA%\DraftAssistant + this checkout
uv run python measure_storage.py "%LOCALAPPDATA%\DraftAssistant" C:\path\to\old\lol-draft-assistant
```

It reports:
- `cache/`: total size, files per subfolder, `riot/` by host
- a 400-file compression test: gzip-6/9, zlib with a 32 KB preset dictionary (stdlib), lzma, and zstd if it's already installed
- each `*.sqlite` file and its WAL, free pages
- `dbstat` per table and index, with bytes per row
- bytes of `match_id` text per table
- indexes, and which tables are `WITHOUT ROWID`
- matches per patch × tier × queue
- distinct keys per (tier, queue) on the newest patch: role stats, lane pairs, synergy pairs, rune pages, items, opponent × keystone. These are the inputs to item 4. `--quick` skips them; the self-joins take a minute or two on a big database.

**Found from the schema, before any numbers:**
- **Composite primary keys are stored twice.** No table is `WITHOUT ROWID`, so every composite-PK table (`match_participants`, `match_builds`, `match_build_events`, `match_frames`, `matchups`, `synergy_pairs`, ...) keeps a hidden rowid B-tree plus a `sqlite_autoindex_*` copy of the key. On a synthetic copy the autoindex was about 50% of the table's size for `match_participants` and `match_builds`. This is likely the biggest database saving in Stage 1.
- `match_builds` has two secondary indexes (`champ_id`, `keystone`), and `match_build_events` has one. Whether the Strategy queries use them goes through `EXPLAIN QUERY PLAN` in Stage 1.
- **Timelines are not cached** (`cache_body=False`). So `cache/riot/europe` holds match bodies (around 25–40 KB of JSON each), plus small id lists and league pages. Match JSON usually compresses 6–9× with gzip. The script measures your real ratio.

## 2. Read inventory

Runtime = Live, Prep, Review, Strategy, Roster and the Crawl page, i.e. anything under `api/`, `engine/`, `learning/`,
and the loaders those call (`provider.load_engine_inputs`, `refresh_stats`, `api/strategy.py`).

### Runtime crawl tables (the ones to share)

| Table | Read by | Kind |
| --- | --- | --- |
| `champ_role_stats` | `provider.bracket_stats`, `matchup_model.pooled_stats`, `synergy.load_damage`, `profiles` | **Additive**: games, wins, picks, bans, short/long games. The `dmg_*` columns are means, so they have to be shared as sums plus a denominator |
| `matchups` | `bracket_stats`, the Matchup tree (`pooled_stats`), lane bans, History recap | **Additive**: games, wins, gold/XP sums, lane_n |
| `bracket_totals` | `bracket_stats`, `stats_version` | **Additive** |
| `synergy_pairs`, `synergy_role_stats` | `synergy.load_synergy` (Live, Prep) | Stored today as **decayed pooled windows** (`games_w`, `wins_w`), so not additive as stored. Additive once kept per patch (new per-patch count tables), with the window then summed locally using `window_weights` |
| `champion_traits` | `traits.load_traits` (synergy v2, tags, Live) | **Non-additive**: weighted means and percentiles per window |
| `matchup_fits` | `load_matchup_model` (every matchup, bans, tree) | **Non-additive** (per window) |
| `synergy_fits` | `load_synergy_model`, `build_evaluator` | **Non-additive** (per window) |
| `execution_fits` | `load_draft_scale` (Live lock-in), History execution | **Non-additive** |
| `power_curves` | `api/strategy.py` | **Non-additive**: smoothed against theory |
| `match_builds` + `match_participants` + `crawled_matches` | `builds.summarize` (Strategy runes, items, spells; **filtered by lane opponent**) | **Per-match rows at runtime.** Needs new additive build-count tables, and Strategy's query changes to read them (see ASK 6) |
| `match_build_events` | `builds.summarize_order` (Strategy item/skill order) | **Per-match rows at runtime.** Needs aggregated order counts (first items in order, skill-max order, level timings as sums) |
| `match_participants` (team damage) | `builds.damage_thresholds` (Strategy "damage-heavy") | **Non-additive** (quantiles): share the three thresholds as a fit |
| `crawled_matches` | `builds/_weights`, `power._weights` (`DISTINCT patch`), Crawl page tier table, `learning_routes` (patch counts) | Replace with `bracket_totals` per patch (additive) |
| `master.sqlite`: the same tables | Matchup tree, Strategy "what Master+ runs" | As above, tier `MASTER_PLUS` |
| `bootstrap_role_stats` | `provider.load_bootstrap` | Not crawl data: each machine fetches OP.GG itself. Stays local |
| `champions`, `champion_tags` | everywhere | Static (Data Dragon) or from YAML: stays local |

### Crawl, fit and job tables only (never shared; users don't need them)

`crawled_matches` (except the counts above), `match_participants`, `match_bans`, `match_team_state`, `match_frames`,
`match_draft_logodds`, `seed_players`.

### Local tables (own data, never shared)

`players`, `pool_entries`, `drafts`, `draft_actions`, `player_games`, `own_game_participants`, `game_execution(_roles)`,
`scout_players`, `scout_games`, `app_settings`.

**Consequence:** "read path unchanged" holds in that everything still reads local SQLite. But Strategy (builds, order,
damage thresholds) and the four `DISTINCT patch FROM crawled_matches` lookups must change their SQL to read aggregate
tables, because users won't have the per-match rows. That's the one place runtime code changes, and it's also why the
admin has to keep the per-match tables.

## 3. Turso client on Windows

| Option | Windows / Python 3.12 | Status | Verdict |
| --- | --- | --- | --- |
| `pyturso` 0.8.3 (12 Oct 2026; 0.9.0rc1 on 15 Oct) | **No Windows wheel in any release.** Only macOS and manylinux wheels plus an sdist, which needs a Rust toolchain to build. 0.9.0rc1 is macOS-only | Young (the Rust rewrite) | Not viable for the installer |
| `libsql` 0.1.11 (2 Sep 2025) | Has a `cp312-win_amd64` wheel | No release in 14 months. Embedded replicas mirror the **whole file** from Turso, so local-only tables (drafts, pools) would need a second ATTACHed file. Writes go to the primary | Works in principle, but a stale native dependency, a second database file on Live's read path, and an unknown PyInstaller story |
| `sqlalchemy-libsql` | README: Linux and macOS only | | No |
| **HTTP API over `httpx`** (Hrana `POST /v2/pipeline`; Platform API `POST /v1/organizations/{org}/databases/{db}/auth/tokens?expiration=90d&authorization=read-only`, `.../auth/rotate`) | Pure Python, already bundled | Stable public API | **Recommended** |

The evidence is wheel metadata from PyPI, checked today. I couldn't run Windows here. Publish and pull are bulk batch jobs, not
live queries, so a replica brings nothing we need. Over HTTP: no new dependency, nothing new in the PyInstaller bundle,
and `FakeSharedStore` is trivial because the interface is just "run these statements".

## 4. Turso quota estimate

**Free tier today** (turso.tech/pricing, fetched 8 Nov 2026): 100 databases, 5 GB storage, 500 M rows read per month,
**10 M rows written per month**, 3 GB syncs. **Developer** ($4.99 per month): 9 GB, 2.5 B reads, **25 M writes**, then $1 per extra million.

**Estimate per patch** at the coverage target (about 20k matches per tier, split over solo and flex, 6 tiers plus Master+). These are rough figures until the script's cardinalities arrive:

| Shared table, per (patch, tier, queue) | Rows per (tier, queue) | × 14 (tier, queue) |
| --- | ---: | ---: |
| role stats | ~700 | ~10k |
| matchups (lane pairs) | ~10k | ~140k |
| synergy pairs (per patch) | ~45k | ~630k |
| builds: pages, spells, items, opponent × keystone | ~25k | ~350k |
| build order and level timings | ~5k | ~70k |
| **total** | | **~1.2 M rows per patch** |

Fits per dataset (matchup, synergy and execution fits, traits, power curves, thresholds): about 30k rows × 4 datasets ≈ 120k.

**"Changed rows only, after each crawl session":** a night's crawl changes nearly every count row of the current patch,
because counts go up. So each publish rewrites about 1.2 M rows, plus refitted fits.

| Publishing | Rows written per month | Free (10 M) | Developer (25 M) |
| --- | ---: | --- | --- |
| Row level, nightly | ~40 M | ✗ | ✗ (~$15 extra, ~$20 total) |
| Row level, weekly | ~6–7 M | ✓ at 65%, but the 70% warning would be near | ✓ |
| Row level, nightly, pooled tiers only for synergy | ~20 M | ✗ | ✓ but tight |
| **Chunked**: one row = one gzip blob of a (patch, tier, queue, table) slice, ≤ 1 MB | **~10k** (a few hundred rows per publish) | ✓ (0.1%) | |

Storage: a patch is about 40 MB as rows, or about 8–15 MB compressed. Twenty patches fit in either form (5 GB).
Reads: a user's first full pull is about 25 M rows at row level (five users fit within 500 M) or a few thousand chunks.

**Recommendation: chunked publishing on the free tier.** Each chunk row carries `publish_id`, a content hash (so "changed
only" works per chunk, and an unchanged chunk isn't written), the schema version and the slice key. Pull decodes chunks
and upserts into ordinary local tables, with the same names as Turso's would have had. Cost: you can't run ad-hoc SQL over the shared
stats in Turso's console. The identity test decodes every chunk before scanning.

---

## Plan (after the ASKs are answered)

Every stage starts with failing tests and ends with `uv run pytest` green, plus a stop for your review.

**Stage 1: storage (local).**
- `data/response_cache.py`:
  - `ResponseCache` over `cache.sqlite` (the spec's schema, `WITHOUT ROWID`, codec per row)
  - read order: packed row, then the legacy `.json` file (so a half-migrated cache works), then the archives
- `RiotClient.get`, `backfill._cached_match` and `timelines.estimate_refetch` all go through it.
- `draftassist cache-pack`:
  - walks the files in batches of 2,000, one transaction each
  - re-reads and compares canonical JSON before it deletes any file
  - resumable, because a file is only deleted after its row is committed and verified
  - prints sizes before and after
- `cache-archive --older-than-patches 6`:
  - match bodies carry `gameVersion`, so they can be moved by patch
  - other entries (ids, leagues) stay put
- Databases:
  - `draftassist db-optimize --dry-run` reports `dbstat` savings on a scratch copy for each change: `WITHOUT ROWID`, an integer match key (only if the numbers justify it), and the indexes no `EXPLAIN QUERY PLAN` uses
  - it then applies the changes through `db/session.py`, as a table rebuild with copy and rename, followed by VACUUM
  - it refuses while `cache/jobs/running.json` shows a crawl
- Tests: round-trips, a mixed cache, a crash in the middle of a batch (killed between commit and delete, and before commit), backfill identical from a packed cache.

**Stage 2: publish and pull.**
- New per-patch count tables that aggregation fills alongside today's:
  - synergy pairs and role stats per patch
  - build counts
  - order counts
  - the damage sums
  - a queue column on the count tables
- Today's windowed tables are then derived from them, and Strategy reads the aggregates.
- `data/shared/`:
  - `SharedStore` protocol with `HttpSharedStore` (httpx) and `FakeSharedStore`
  - `publish.py`: hash chunks, write changed ones in batches, tombstones, the quota meter, resumable through a local `publish_state`
  - `pull.py`: fetch chunks newer than the local `publish_id`, then decode and upsert in one transaction, then `refresh_stats`
  - `schema.py`: `SCHEMA_VERSION`, the table registry, and the identity guard used by publish and by the tests
- Gates on both: never during champ select, reusing the updates' check.
- Tests as listed in the task.

**Stage 3: sign-in.**
- `data/shared/account.py` handles roles from the stored credentials, invite encode/decode (`dav1.` + base64url JSON with the URL, token, name and expiry) and a Platform API client (mint, rotate, re-mint everyone).
- Credentials go in `keyring` if you agree (ASK 4); otherwise a `credentials.json` in the data folder, never in `.env` (the importer copies `.env`).
- `members` table.
- Role-gated routes return 403, and the CLI refuses admin commands.
- The Crawl page splits by role.
- Token redaction in logging and job output, with a test that greps `cache/jobs/`.

**Stage 4: datasets.**
- The `datasets` table and the four defaults, with the admin running fits for each dataset (window = dataset_id).
- The user selection lives in `app_settings`.
- The additive tables are summed into the local windowed tables.
- Closest dataset: same scope first, then tier overlap (Jaccard), then the dataset id as a tie-break. Shown as a "Fits from" label.

**Stage 5: installer and first run.**
- An Inno page with three choices.
- A first-run pull with progress.
- `import-data` skips crawl tables in user mode.
- Account switching.
- Packaging tests.

**Stage 6: docs and the decision log.**

## ASKs

1. **Turso client:** HTTP over `httpx` (no new dependency), as above. *Recommended.* The alternative is `libsql` 0.1.11.
2. **Quota:** chunked publishing on the free tier. *Recommended.* Alternatives: row-level on Developer (~$20 a month at nightly publishing), or row-level weekly on free.
3. **zstd:** decide from the script's numbers. My rule: add `zstandard` only if it beats gzip-6 by ≥1.4× on your cache; otherwise gzip, or stdlib zlib with a preset dictionary if that column wins.
4. **keyring** (Windows Credential Manager; adds `keyring` + `pywin32-ctypes` + `jaraco.*`, all pure Python with a PyInstaller hook). *Recommended yes.*
5. **Refit locally (Stage 4):** later. *Recommended.* Can be done from counts alone: matchup fits and the synergy style and team rules. Needs per-match rows: traits percentiles, execution fits, power curves (frames), damage thresholds.
6. **Strategy builds by opponent:** share opponent × keystone and opponent × first item only. *Recommended.* Full opponent × page × items is about as big as the per-match rows. Rune pages, items and spells stay opponent-agnostic.

## Decisions (8 Nov 2026)

1. **Turso client:** HTTP API over `httpx` (Hrana pipeline plus the Platform API). No new dependency.
2. **Quota:** compressed blocks on the free tier. One shared row is one gzip block per (patch, tier, queue, table), with a content hash and `publish_id`. Add a `draftassist shared-inspect` command that decodes and shows what's published, because Turso's console only shows blocks.
3. **Codec:** decide from `scripts/measure_storage.py` on the real cache. Add `zstandard` only if it beats gzip-6 by at least 1.4×. Otherwise use gzip, or stdlib zlib with a preset dictionary if that wins.
4. **Credentials:** `keyring` (Windows Credential Manager). The data-folder file is the fallback only where keyring is unavailable.
5. **Refit locally:** later. Users take fits from the closest published dataset.
6. **Strategy builds:** no counter-builds for the sake of it. Strategy shows the overall builds and runes, and players choose their own. On top of that it may *suggest* individual items or rune choices that look helpful in this lane, as optional hints. A hint needs enough games and a real gain against this opponent (lane lead or win rate) over the overall build, so most lanes get none. Shared data is therefore overall pages, items and spells, plus opponent × item and opponent × rune counts, used only for those hints. This changes Strategy's behaviour on the admin's machine too.

## Measured (8 Nov 2026, on the admin's machine)

From `scripts/measure_storage.py`. zstd was measured in a throwaway environment (`uv run --no-project --isolated --with
zstandard`), so nothing was added to the project. Row counts and block sizes come from a read-only pass over the 30,434
cached match bodies of patch 16.21, joined to `crawled_matches`. The local databases predate `match_builds`, so the script
couldn't count builds from them. 16.22 had only 201 low-elo matches, so 16.21 stands in for a full patch.

### Sizes

| Location | Cache | `draftassist.sqlite` | `master.sqlite` |
| --- | --- | --- | --- |
| `%LOCALAPPDATA%\DraftAssistant` | 254 MB, 421 files: `updates/` 239 MB (5 files), `riot/` 9.2 MB, `ddragon/` 5.7 MB | 81.8 MB + 4.1 MB WAL | 34.8 MB |
| old `lol-draft-assistant/` | **3.06 GB, 50,396 files**: `riot/europe` 3.04 GB (50,179 files, mean 60.7 KB), `riot/euw1` 3.9 MB, `ddragon/` 5.2 MB | 81.5 MB + 0.9 MB WAL | 34.8 MB + 11.8 MB WAL |

- `draftassist.sqlite`, largest objects: `match_participants` 18.8 MB, `synergy_pairs` 16.5 MB + its autoindex 14.6 MB, `match_bans` 5.9 MB + autoindex 6.4 MB, `match_participants` autoindex 5.9 MB. The `sqlite_autoindex_*` copies add up to about 31 MB of the 82 MB, which is the `WITHOUT ROWID` saving to confirm in Stage 1.
- `match_id` text: 2.9 MB each in `match_bans` and `match_participants`, under 1 MB elsewhere. That's about 15% of the file once the autoindex copies are counted, and most of it goes when `WITHOUT ROWID` removes the copy. These are crawl-only tables (users never hold them), so the integer match key isn't worth it. Stage 1 confirms on the scratch copy.
- Free pages: 0 in every database, so `VACUUM` alone gains nothing.
- `updates/` (239 MB of downloaded installers) is outside this task. It's a one-line cleanup after a successful update.

### Codec (decision 3)

400-file sample, train on half, test on the other half. Ratios are against compact JSON, which is 0.92× the on-disk size.

| Codec | Ratio | ms/file | 3.04 GB `riot/` packed |
| --- | ---: | ---: | ---: |
| gzip-6 | 6.8× | 0.71 | ~410 MB |
| gzip-9 | 7.0× | 0.92 | |
| zlib-6 + 32 KB preset dictionary (stdlib) | 8.7× | 0.89 | ~320 MB |
| lzma-6 | 9.0× | 9.2 | |
| zstd-3 | 7.3× | 0.09 | |
| zstd-19 | 8.7× | 29 | |
| **zstd-3 + trained dictionary** | **10.3×** | **0.10** | **~270 MB** |

Rule: `zstandard` only if it beats gzip-6 by at least 1.4×. zstd-3 with a dictionary does (10.3 / 6.8 = 1.51×), and plain zstd-3
doesn't (1.07×). So it's **zstd-3 + a trained dictionary**, codec `zstd-d1`. The dictionary is stored in `cache.sqlite` and
never changed once rows use it. gzip stays a readable codec. Against the best stdlib option (zlib + dictionary) the gain is
1.18×, about 50 MB, and the speed difference doesn't matter at crawl rates. Python 3.14's stdlib `compression.zstd` reads the same format, so
the dependency can go when the project moves to 3.14. Shared blocks stay gzip as decision 2 says: at 5–970 KB a block, a dictionary adds little.

### Rows per (tier, queue), patch 16.21

Distinct keys, which is the number of rows in each would-be shared table. Role is `teamPosition`, so off-role picks count.

| tier / queue | matches | role | lanes | pairs | pages | spells | items | opp × keystone | opp × rune | opp × item |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| iron / 420 | 2,823 | 640 | 4,863 | 19,457 | 7,597 | 1,961 | 16,597 | 13,583 | 79,985 | 86,964 |
| bronze / 420 | 2,870 | 623 | 4,907 | 19,648 | 7,394 | 1,723 | 15,989 | 13,391 | 79,852 | 88,732 |
| silver / 420 | 2,704 | 595 | 4,883 | 19,433 | 7,119 | 1,533 | 15,428 | 13,068 | 78,009 | 87,550 |
| gold / 420 | 2,513 | 590 | 4,858 | 19,287 | 7,283 | 1,439 | 15,016 | 12,700 | 76,568 | 85,023 |
| platinum / 420 | 2,608 | 596 | 4,982 | 19,779 | 8,187 | 1,416 | 15,142 | 13,008 | 79,285 | 88,332 |
| emerald / 420 | 2,580 | 568 | 4,987 | 19,835 | 9,115 | 1,348 | 14,821 | 12,819 | 79,658 | 88,249 |
| master / 420 | 11,050 | 672 | 10,053 | 39,629 | 32,818 | 2,069 | 24,338 | 30,416 | 197,293 | 237,781 |
| iron / 440 | 411 | 443 | 1,469 | 5,798 | 1,681 | 791 | 6,699 | 3,275 | 19,491 | 18,716 |
| bronze / 440 | 362 | 420 | 1,385 | 5,467 | 1,619 | 762 | 6,277 | 3,048 | 18,149 | 17,122 |
| silver / 440 | 535 | 446 | 1,898 | 7,582 | 2,127 | 839 | 7,685 | 4,223 | 25,364 | 24,940 |
| gold / 440 | 710 | 514 | 2,375 | 9,431 | 2,804 | 1,003 | 9,238 | 5,338 | 32,293 | 31,954 |
| platinum / 440 | 626 | 474 | 2,174 | 8,637 | 2,521 | 885 | 8,340 | 4,844 | 29,221 | 28,726 |
| emerald / 440 | 642 | 489 | 2,250 | 8,926 | 2,697 | 924 | 8,533 | 4,985 | 30,290 | 29,626 |
| **total** | **30,434** | **7,070** | **51,084** | **202,909** | **92,962** | **16,693** | **164,103** | **134,698** | **825,458** | **913,715** |

There are 13 slices, not 14: the Master+ crawl is solo queue only. The opponent tables (decision 6) are 77% of the rows,
against about 40k rows for role stats plus lane pairs. Build order, level timings and damage sums couldn't be counted, because
timelines aren't cached and these databases have no `match_build_events`.

### Blocks and Turso estimate (decision 2)

One gzip-6 block per (patch, tier, queue, table), serialised as compact JSON arrays with realistic count and sum columns:

| Table | Blocks | gzip total | Largest block |
| --- | ---: | ---: | ---: |
| role | 13 | 0.05 MB | 5 KB |
| lanes | 13 | 0.22 MB | 46 KB |
| pairs | 13 | 0.81 MB | 166 KB |
| pages | 13 | 0.59 MB | 186 KB |
| spells | 13 | 0.08 MB | 10 KB |
| items | 13 | 0.69 MB | 111 KB |
| opp × keystone | 13 | 0.57 MB | 135 KB |
| opp × rune | 13 | 3.13 MB | 799 KB |
| opp × item | 13 | 3.62 MB | **974 KB** (master/420) |
| **measured** | **117** | **9.75 MB** | 2.41 M rows inside |

- **Per patch today:** about 117 blocks plus about 50 unmeasured ones (bracket totals, order counts, level timings, damage sums), so **~170 blocks, ~11 MB**.
- **Fits:** about 8 blocks per dataset, ~32 for the four defaults. `execution_fits` is one 1.9 MB row raw: check its gzip size, and split it if it's over 1 MB.
- **Block size:** master's opponent × item block is already at the 1 MB cap with 11k matches. The two opponent tables are therefore split by role (5 blocks per slice), which brings a patch to about **250 blocks**.
- **Writes:** a nightly publish rewrites the current patch's blocks plus the fits, about 280 rows. That's **~9k rows per month, 0.09% of the free tier's 10 M**. Even hourly publishing during a crawl stays under 1%.
- **Storage:** ~11 MB per patch today. At the target of about 20k matches per tier, sublinear growth from master's numbers suggests 25–40 MB per patch. Twenty patches is therefore **0.2–0.8 GB of the 5 GB**.
- **Reads:** a pull is `WHERE publish_id > ?` with an index on `publish_id`. That's about 280 rows a night, and a first full pull is ~5k rows. Against 500 M a month, that's nothing.
- **Download:** a first full pull is ~11 MB per patch today.

## Decision 6: the "helpful in lane" rule (agreed 8 Nov 2026)

Strategy shows the overall build. Against a known lane opponent, an option is a hint only if all of these hold:
- **The option** is one of: a rune of the page, a starting item, or the first finished item. End-of-game items mostly show who was already winning, so they don't count.
- **At least 30 weighted games** with the option against this opponent, and at least 30 without it.
- **It isn't already in the overall build:** the page's runes, the top three items, or the usual starter.
- **A gain** of at least +300 gold at 15 minutes, or at least +3 points of win rate, over the same champion's other games against that opponent.
- **z ≥ 3.** About 70 options are tested per lane, so chance alone gives about 0.1 false hints per lane.

At most 2 hints are shown, ordered by z.

The data comes from `builds.lane_hints` and `builds.lane_hint`, over `cnt_lane`. All tiers of the database are pooled, and patches are weighted as the builds are. The gold lead is stored as a count, a sum and a sum of squares, so its mean and spread add up across patches and tiers.

## Stage 2 notes (built 8 Nov 2026)

- **The admin's tables are derived the same way a pull derives them.** Aggregation builds the per-(patch, tier, queue) counts and runs the same `counts.derive` a pull runs, so its runtime tables are reproduced exactly on a teammate's machine. A scratch copy of the real databases confirmed it: 174 blocks, 12.1 MB gzipped, publish 19 s, pull 19 s, and both databases identical after the pull.
- **Strategy reads the counts on every machine**, so these Strategy behaviours changed on the admin's machine too:
  - Items that tie on share are ordered by item id. Before, slot order decided.
  - An item held twice counts once.
  - The median second of the first item and of level 6 now comes from 10-second bins.
  - The vs-opponent build switch is gone (decision 6).
- **Count reads are primary-key seeks:** the queries go through the `cnt_bracket` slices with `CROSS JOIN`. It's 19 ms per champion with an opponent on the real data, and `tests/test_db_optimize.py` checks the plans.
- **Older synergy windows are deleted when a newer one is built.** Nothing read them.
- **`champions.roles` follows the window patch.** Before, an older patch being aggregated could set it.
- **The champion indexes on `match_builds` and `match_build_events` are dropped by `db-optimize`.** Strategy no longer reads those tables.
- **Fixed on the way:** `backfill_builds` failed past 2,000 matches. The connection it used was closed by its own periodic commit.

## Stage 3 notes (built 8 Nov 2026)

- **Credentials** are in `data/shared/account.py`.
  - Each secret is its own Windows Credential Manager entry (keyring, service `DraftAssistant`), named by key and a hash of the data folder, so a second `DRAFTASSIST_HOME` is a separate account. `credentials.json` in the data folder is the fallback only where no keyring works.
  - `account.json` holds the role, name, URL, organisation, database and expiry: nothing secret.
  - `import-data` copies neither file.
- **Invites** are `dav1.` + base64url JSON: URL, read-only token, name and expiry. The default expiry is 90 days, with warnings from 14 days ahead.
- **Members** live in the shared database's `members` table: name, role, the token's sha256, created, expires, revoked. It's there for the UI; Turso's token permissions are the real enforcement.
  - **Remove:** rotates the keys, mints a new full-access token for the admin (stored, and the store is reopened with it), re-mints for everyone left, and shows their new codes once.
  - **Renew:** re-mints one member's token without rotating.
- **Roles:**
  - **admin:** a full-access token plus the Platform API token.
  - **user:** a read-only token from an invite.
  - **solo:** nothing stored.
  - **What a user can't do:** the crawl-data jobs and commands are refused with a 403 or exit code 2 (crawl, timelines, refetch, Master+, fit, power, backfill, coverage, publish, members).
  - **What a user keeps:** the own-game jobs.
  - **Solo mode** is today's behaviour, without Publish.
- **No token in logs:** every secret the account loads or mints is registered, and every log record is redacted as it's created (`data/shared/redact.py`). Tests check job logs, `account status` output, a repo scan for token-shaped strings, and the bundle when one is built.
- **Not done yet:** the installer pages (Stage 5) and Datasets on the Crawl page (Stage 4).

## First live run (8 Nov 2026)

- **Publish 1 is on Turso:** 174 blocks, 12.09 MB, 175 rows written. A test pull from Turso into an empty folder gave tables identical to the admin's, for both the main and Master+ databases.
- **Found live, and fixed with tests:**
  - **Turso sends blob base64 without `=` padding.** Every real pull failed on its first block. The client now pads.
  - **A token minted straight after a key rotation was refused for good;** one minted a minute later worked. So after a rotation ("Remove member"), every new token is checked against the database and minted again until it works.
- **The keys were rotated once,** at the admin's request, to kill a never-expiring read-only token that had sat in a text file. The admin's new full-access token is in Credential Manager.

## Stage 4 notes (built 8 Nov 2026)

- **Datasets** are rows in a shared table, published with the fits. The defaults:
  - `current`: the current patch
  - `recent`: the last 6 patches, half-life 6
  - `all`: every patch, equal weights
  - `master_plus`: the Master+ database's own fits, for the Matchup tree

  The admin adds more on the Crawl page (Datasets).
- **Before each publish, the admin's machine fits every dataset** under the window `ds:<id>` (`datasets.fit_datasets`): synergy window, traits, damage thresholds, synergy and matchup fits.
  - **Data used:** a dataset's fits pool every tier and both queues, as all fits always have; tiers and queues shape only the additive tables.
  - **Synergy windows:** each dataset's is only for fitting and is dropped afterwards.
  - **Skipped** when neither the counts nor the datasets changed.
  - **Code:** the fit code itself is unchanged; `synergy.crawl_weights` gives a dataset window its patch scope.
- **A teammate's selection** (Crawl page, Data selection): patch scope (the admin's choice, current, last N, specific patches or all) with a half-life, queues, tiers to pool for each player bracket, and Master+ for the tree on or off.
  - **Where it goes:** `datasets.apply_selection` sums the pulled counts for exactly that choice into the runtime tables under the patch key `sel`, so the per-patch rows the Matchup tree weighs stay as pulled.
  - **Synergy and fits:** the synergy window is derived from the counts under the closest dataset's window, which holds that dataset's pulled fits. Every loader follows `synergy.latest_window`, which returns the active window.
  - **The "Fits from" label** names the dataset in use.
  - **The default selection** changes nothing: the admin's current window, exactly as in Stage 2.
  - **Timing:** it's applied between drafts; a choice made during champ select waits for the next stats refresh after it. It's also re-applied after every pull.
- **Closest dataset:** the same patch scope first (for last N, the nearest N), then the most patches in common, then the most tiers in common, then the dataset id.
- **Not done:**
  - **The "minimum games" setting.** The existing thresholds stay as they are.
  - **Per-dataset execution fits.** The fit job still fits the current window only, so a selection uses the newest patch window's execution fit.
  - **Refitting locally.** That's later, per decision 5.
