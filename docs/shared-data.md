# Task: shared crawl data on Turso, admin and user sign-in, storage optimisation, installer changes

Read `CLAUDE.md`, `docs/SPEC.md`, `packaging/README.md` and the code map first. This is a big change, so it goes in stages, and you stop after each stage for my review. Plan first, show me the plan, then build. Write tests first. Several decisions below are marked **ASK**: bring them to me with your recommendation, don't decide them yourself.

## Goal

Today every install (mine and my four teammates') has its own SQLite databases and `cache/`, so each one would need its own overnight crawl. After this task:
- **One admin** (me) crawls, fits and **publishes** the crawl's statistics to one shared Turso database.
- **Users** (teammates) sign in with an invite, **pull** those statistics into their local SQLite, and choose which datasets their app uses (patches, tiers and so on). They can't run crawls or write to the shared database.
- Live, Prep and Review keep reading **local SQLite** exactly as now, so champ select never waits on the network and the app works offline from its last pull.
- Storage is smaller on every machine: the admin's cache is compressed and packed, and users never hold raw crawl data.

## Hard rules for this task (in addition to CLAUDE.md's)

- **No credentials in the repo or the installer.** The repo is public (releases need it). Turso database tokens, the Turso Platform API token and the Riot key live only in each machine's data folder (`%LOCALAPPDATA%\DraftAssistant`), preferably in Windows Credential Manager (see Stage 3). Never log or print them. Add a test that scans the bundle and the repo for token-shaped strings.
- **No identities in the shared database.** No PUUIDs, Riot IDs, summoner names, our drafts, our own games, scouting data, roster, pools or settings. Riot encrypts PUUIDs per API key (CLAUDE.md lessons), so they would be useless to teammates anyway. Shared tables hold aggregate statistics and fits only. Add a test that fails if any shared table has a column named like `puuid`, `riot_id`, `game_name`, `tag_line` or `summoner`, or holds a value that looks like a Riot ID or PUUID.
- **Read path unchanged.** Engine and API code keep reading the local database. Only the new sync layer talks to Turso.
- **Offline-safe.** If Turso can't be reached, the app uses the last pulled data and shows "Stats from <date>" quietly. It never blocks Live.
- **Read-only League client** rule unchanged.

---

## Stage 0: measure and spike (report back, change nothing)

1. **Sizes.** For my data folder (and the old subfolder if it still exists), report:
   - total size of `cache/`, number of files, the largest subfolders by host
   - each `*.sqlite` file's size
   - the top tables by size (SQLite `dbstat`)
2. **Read inventory.** List every table the app reads at runtime (Live, Prep, Review, Strategy, Roster) versus tables only the crawl, fits and jobs touch. For each runtime table, say whether its rows are **additive** (counts like games and wins per patch and tier, which can be summed across patches and tiers) or **non-additive** (fits, percentiles, smoothed values).
3. **Turso client on Windows.** Check whether `pyturso` (`turso.sync`, push/pull) and `libsql` (embedded replicas) install and work on Windows 11 with Python 3.12, **and** inside the PyInstaller bundle. Note: `sqlalchemy-libsql`'s README says it supports only Linux and macOS. If neither works reliably, the fallback is a small sync layer over Turso's HTTP API using `httpx` (already in the stack). Recommend one, with evidence. **ASK** before adding any new dependency.
4. **Turso quota estimate.** From the current crawl rate (about 1,800 matches per hour) and the table inventory, estimate rows written per month to Turso under "publish aggregates and fits, changed rows only, after each crawl session", and storage. Compare these with the current Turso free tier (check turso.tech/pricing; third-party sources disagree). If it doesn't fit, propose the cheapest way that does (coarser publishing, fewer tables, or the paid Developer plan). **ASK**.

---

## Stage 1: storage optimisation (local; useful even without Turso)

**Response cache** (`data/riot.py`; today one plain `.json` file per request, with a `{fetched_at, key, body}` wrapper):
- Move it into one `cache.sqlite` (`key TEXT PRIMARY KEY, host TEXT, fetched_at REAL, key_id TEXT, codec TEXT, body BLOB`, `WITHOUT ROWID`), with the body compressed.
  - Default codec: `gzip` from the standard library (no new dependency).
  - `zstd` is an option only if Stage 0 shows a worthwhile gain. **ASK** before adding it.
  - Keep the codec per row so they can be mixed.
- `RiotClient.get` keeps its interface, and `data.backfill` and everything else read through the same accessor.
- A migration command (`draftassist cache-pack`) converts the existing files and verifies every row decodes to the same JSON. Only then does it delete the originals, unless `--keep`. It is resumable and prints before/after sizes.
- An optional archive (`draftassist cache-archive --older-than-patches 6`) moves rows for old patches into `cache-archive-<patch>.sqlite`, which can be zipped, and that backfill can still read. Default off.

**Databases:**
- Report the savings of each change (using `dbstat` on a scratch copy) before applying it:
  - `WITHOUT ROWID` for tables keyed only by a composite primary key
  - an integer match key instead of repeating the text `match_id` in every child table, **only if** Stage 0 shows match_id columns are a meaningful share
  - dropping unused indexes (check with `EXPLAIN QUERY PLAN` on the hot queries)
  - `VACUUM` after
- Apply them through the existing migration path in `db/session.py`, and never on a database a crawl is writing to.

**Tests:** pack/unpack round-trips byte-identical JSON; a mixed old-files and packed cache reads correctly during migration; a crash halfway through migration loses nothing; backfill gives identical results from a packed cache.

---

## Stage 2: shared database, publish (admin) and pull (user)

**Two kinds of shared data:**
1. **Additive count tables, per (patch, tier, queue):** role stats, matchups, synergy pairs, bracket totals, aggregated builds (counts of rune pages, items and spells per champion and role, not per-match rows) and the Master+ equivalents (tier `MASTER_PLUS`, so the separate `master.sqlite` doesn't need to be shared). Because these are counts per patch and tier, a user can combine any patches and tiers locally by summing (Stage 4).
2. **Non-additive results, per published dataset:** matchup fits, synergy fits, execution fits, champion traits and percentiles, smoothed tables. These need the full fitting pipeline, so the admin computes them for a few **datasets** (Stage 4) and publishes each one.

**Schema:**
- Shared tables have a `shared_` prefix in Turso and keep the same names locally, plus:
  - `publish_log` (`publish_id`, `published_at`, `app_version`, `schema_version`, `dataset_ids`, `rows_written`)
  - `datasets` (Stage 4)
  - `members` (Stage 3)
- Every shared row carries `publish_id`, so pulls can be incremental.
- `schema_version`: an app that's too old to read a newer schema refuses that pull and suggests updating, and keeps its current data.

**Publish (admin only):** `draftassist publish` and a button on the Crawl page.
- Runs after a crawl or fit.
- Computes row hashes locally and writes **only changed rows**, in batched transactions, together with a tombstone for removed rows.
- Prints the rows written against the monthly quota from Stage 0 (warn at 70%, stop at 95%, configurable).
- Never runs during champ select (same rule as updates). Resumable.

**Pull (users, and the admin's other machines):** on app start, then every few hours, plus a "Sync now" button.
- Downloads rows newer than the local `publish_id` into the local database inside one transaction, then refreshes the in-memory stats the way the existing stats refresh does.
- Never during champ select.
- On failure, keeps the local data and retries later.

**The sync layer** (`data/shared/`): `SharedStore` is an interface with the implementation chosen in Stage 0 (SDK or HTTP), plus a `FakeSharedStore` for tests. Nothing outside `data/shared/` imports the Turso client.

**Admin's own machine:** keeps the full crawl databases and cache locally as now. Publishing is a copy out, not a move.

**Tests:**
- Publish then pull on a fake store reproduces the admin's runtime tables exactly.
- A second publish with no changes writes 0 rows.
- An interrupted pull leaves the local data unchanged.
- Too-new schema handling.
- The identity-free test from the hard rules.
- Live builds stay within their current time budget after a pull.

---

## Stage 3: sign-in, roles and members

Turso has no username/password login without a separate auth server, so **sign-in is an invite code**:
- The admin creates a member on the Crawl page (display name only, no Riot ID).
- The app uses the Turso Platform API to mint a **read-only database token** with an expiry (default 90 days; Turso supports `authorization=read-only` and an `expiration` on token creation).
- It shows a one-time **invite code**: the database URL plus token, encoded.
- The teammate pastes it in the installer or in Settings → Account. The app stores it in Windows Credential Manager (the `keyring` package; **ASK**), or in the data folder if that's declined.

**Roles:**
- **Admin** = the machine holds a full-access database token **and** the Platform API token. Both are entered once on my machine, never in an invite.
- **User** = holds a read-only token.
- Real enforcement is Turso's token permission: a read-only token can't write. The `members` table (display name, role, sha256 fingerprint of the token, created, expires, revoked flag) is only for showing names, roles and expiry in the UI.
- The app shows "Signed in as <name> (User)" or "(Admin)". It works signed-out too, in **solo mode**: local crawl as today, nothing shared.

**Revoking access:** Turso can only invalidate all of a database's tokens at once (signing-key rotation), not one member's. So "Remove member" means:
1. Rotate the keys.
2. Re-mint tokens for everyone remaining.
3. Show their new invite codes for me to send.

Make that explicit in the UI. Expiring invites show a warning to the member 14 days ahead and on my members list. "Renew" re-mints one member's token without rotating.

**Crawl page by role:**
- **Admin:** everything that's there today (run crawl, fit, backfill, refetch, Master+, coverage, jobs), plus Publish, the quota meter, Datasets (Stage 4) and Members.
- **User:**
  - sync status (last pull, data age, publish history)
  - "Sync now"
  - Dataset configuration (Stage 4)
  - a read-only summary of what's published (patches, tiers, matches per tier)

  No crawl or fit buttons. The CLI commands for crawl, fit, publish and members refuse to run without the admin role, with a clear message.
- **Own-game jobs stay available to users** (postgame, import-games, scouting), because they use the user's own Riot key and their own local tables. They are not crawl data and are never shared.

**Tests:**
- Role detection from the stored tokens.
- User mode hides admin controls, and the API routes refuse them with 403.
- The CLI refuses admin commands for users.
- Invite encode/decode, expiry warnings, the rotate-and-reissue flow on a fake Platform API.
- No token ever appears in logs, responses or the job output under `cache/jobs/`.

---

## Stage 4: datasets (users choose what data they use)

**Admin-published datasets** (the `datasets` table: `dataset_id`, name, patches, tiers, queues, patch weighting, created, status). Defaults to publish:
- `current`: the current patch, all tiers
- `recent`: the last 6 patches, with the existing half-life weighting
- `all`: every crawled patch
- `master_plus`: for the Matchup tree

The admin can add more. Each dataset gets its own published fits (Stage 2, kind 2).

**User configuration** (Crawl page, stored locally in `app_settings`, per machine):
- **Patch scope:** current only / specific patches (multi-select) / last N patches / all, plus the half-life slider where it applies.
- **Tiers to pool**, per player bracket, keeping the per-player bracket setting.
- **Queues:** solo, flex, both.
- **Master+ for the Matchup tree:** on or off.
- **Minimum games** before a stat is shown. Use the existing thresholds as defaults.

**How a selection is served:**
- **Additive tables:** summed locally from the per-(patch, tier, queue) counts for exactly that selection. Fast; it is just SQL over the pulled rows.
- **Non-additive fits:** taken from the **published dataset closest to the selection** (same patch scope first, then tiers). The UI states which one is in use: "Fits from: recent (6 patches)".
- **Refit locally (optional):** runs the existing fit jobs on the user's selection from the pulled counts, only for fits that can be computed from counts alone; list which can and can't. Show the expected minutes. **ASK** whether to build this now or later.

A change of selection triggers the same live stats refresh as today, never during champ select.

**Tests:**
- Summed counts for a multi-patch, multi-tier selection equal a direct aggregation of the same matches.
- The closest-dataset choice is deterministic.
- The "Fits from" label is correct.
- A selection with too few games falls back exactly as the current low-data paths do.

---

## Stage 5: installer, first run and import-data

- **Installer:** asks "How will you use Draft Assistant?"
  - **Join my team (user):** paste invite code; Riot key optional, but needed for own-game history and scouting. Explain both in one line each.
  - **Team admin:** Turso database URL, full-access token, Platform API token, Riot key.
  - **Solo:** today's behaviour.

  Nothing secret is written into the install folder.
- **First run** (`first_run.py`):
  - **Users:** pull the shared data with a progress bar before showing Live. OP.GG stays the fallback only when no shared data has been pulled yet.
  - **Admins:** check the tokens and offer `cache-pack` if an unpacked cache is found.
- **`import-data`:** keeps working. Importing a checkout's databases on a user machine imports only local tables (drafts, own games, roster, pools, settings), not crawl tables, because those come from Turso.
- **Updates:** a new app version with a new shared `schema_version` pulls again after updating. The admin publishes only after updating their own app.
- **Account switching:** Settings → Account: sign out (removes the stored tokens, keeps local data), switch to solo mode, or enter a new invite.

**Tests:** extend `tests/test_packaging.py` so the bundle includes the new modules and dependencies; first run in user mode against a fake store; solo mode is unchanged; `import-data` on a user machine skips crawl tables.

---

## Stage 6: docs and hand-off

- Update CLAUDE.md:
  - Environment: roles, solo mode
  - Commands: `publish`, `cache-pack`, `cache-archive`, members
  - Where files live: `cache.sqlite`, credentials
  - Lessons learned: anything Stage 0 found
- Update `packaging/README.md`, with step-by-step instructions for me (set up Turso, create the database, admin tokens, invite teammates) and for teammates (install, paste invite).
- Write a decision log in this doc for every **ASK**.

## Acceptance (overall)

- `uv run pytest` passes at every stage. The packaging test builds and runs the bundle.
- On my machine: crawl → fit → publish works, the quota meter shows rows written, and cache plus database sizes are lower than the Stage 0 numbers (report both).
- On a second Windows machine (or a second data folder with `DRAFTASSIST_HOME`) with an invite:
  - first run pulls the data
  - Live, Prep and Review show the same suggestions as my machine for the same dataset selection and a replayed draft
  - the Crawl page shows no admin controls, and changing the dataset selection changes the stats
- With the network off, the user machine still runs Live from its last pull.
- The identity-free and no-credentials tests pass.

## Out of scope
- Real username/password accounts (that needs an auth provider such as Turso's bring-your-own-auth JWT integration; maybe later).
- Sharing drafts, History or scouting between teammates.
- A hosted web version of the app.
