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
