# LoL Draft Assistant (Ranked 5s)

Personal, local pick/ban assistant for one 5-stack on EUW. Read `docs/SPEC.md` before doing anything — it is the source of truth for scope, architecture, data model and the phased build plan. Task docs for features beyond the spec live in `docs/` (they sometimes say `docs/prompts/…`; it's the same files).

## How to work
- Build **one phase at a time** from "Build plan for Claude Code" in the spec. Stop at the end of each phase, show the acceptance check passing, and wait for me before starting the next.
- Write `pytest` tests alongside the code; run them before saying a phase is done.
- `reference/draft_engine.py` is a working prototype of the v1 engine. Port its logic into `src/draftassist/engine/`; don't import from `reference/`.
- Ask before adding dependencies outside the spec's stack table, or before changing scope.

## Hard rules
- **Read-only League client.** The LCU connector may only GET and subscribe to events. Never PATCH/POST/PUT/DELETE to the client — no auto-hover, pick, lock, or accept. (`tests/test_readonly_client.py` scans the code for write calls.)
- Secrets live in `.env` (`RIOT_API_KEY`). Never commit `.env`, never print the key.
- Respect Riot API rate limits: one shared rate limiter, cache every response on disk.
- Never commit the database (`data/*.sqlite*`, including the WAL files), raw LCU recordings (`fixtures/lcu/recorded/`) or `tests/screenshots/`: they contain real player names and PUUIDs. To keep a recording as a fixture, run it through `lcu/recorder.sanitize_session` and check no identity value survives.

## Moved to the repo root (7 Nov 2026)
The project used to sit one folder down (`lol-draft-assistant/lol-draft-assistant/`); it's now the repo root. A pull
moves the tracked files, but the local ones stay in the old subfolder: `.env`, `data/*.sqlite*`, `cache/`,
`fixtures/lcu/recorded/`, `tests/screenshots/`, `.venv/`, `web/node_modules/`. Either move the first five up a level (then
`uv sync` and `cd web; npm ci; npm run build`), or install the app and run `draftassist-cli import-data <old subfolder>`
(the installer offers this). The Desktop shortcut to the old `.venv` stops working; the installer makes a new one.
`.gitignore` ignores the old subfolder.

## Environment
- Windows (the League client only runs there). Python 3.12 managed with `uv`; web UI is Vite + Svelte 5 in `web/` (FastAPI serves `web/dist`).
- Run tests: `uv run pytest` (~1,600 tests, ~6 min; the fuzz test and the planted-data fits are the slow part). Tests run with `DRAFTASSIST_STRICT=1`, so suggestion guard violations raise.
- Run the app: double-click **Draft Assistant** on the Desktop. `.venv\Scripts\draftassist-app.exe` starts the server and opens the app in its own window (pywebview on Edge WebView2), with no console; closing the window stops it, and output goes to `cache/launcher.log`. `draftassist.exe` is the same with a console; `--browser` opens the default browser instead (also the automatic fallback without WebView2). Dev: `uv run uvicorn draftassist.api.main:app --reload`.
- Installer and updates (`packaging/README.md`): every push to `main` (except docs-only) publishes a release with the installer (`.github/workflows/release.yml`, version `major.minor.<run number − release_run_base>`: 0.1.x ended at v0.1.10; 0.2.x starts at v0.2.1, the power-spike work); installed apps offer it as a one-click Update (`updates.py`, never from champ select to the end of the game). The installer asks for the Riot key and offers `import-data` from a checkout; the app fetches champions and OP.GG stats itself on first start (`first_run.py`). By hand: `packaging\build-installer.ps1`. The repo must be public for apps to see releases. `DRAFTASSIST_BUILD_TEST=1 uv run pytest tests/test_packaging.py` builds the bundle and runs it.
- After changing the UI: `cd web; npm run build`. After changing Python: restart the app (stats refresh live, code doesn't).
- **Shared data and roles** (`docs/shared-data.md`, built 8 Nov 2026). One **admin** crawls, fits and publishes the crawl's
  counts and fits to one Turso database (`league-drafter`); **users** (teammates) join with an invite code and pull them into
  their local SQLite; **solo** (signed out) is today's app. Live, Prep and Review always read local SQLite, so champ select
  never waits on the network and the app works offline from its last pull ("Stats from <date>" on the Crawl page).
  Tokens live in Windows Credential Manager (`keyring`), never in `.env` or the repo; nothing outside `data/shared/` talks
  to Turso. The admin's Live keeps the admin's own data; a user's Live can follow their own data selection (Crawl page).

### Commands
The **Crawl page** (`#/crawl`, `jobs.py`, `api/jobs_routes.py`) runs the crawl and the other jobs from the app: one at a time, each as its own process with no console (output in `cache/jobs/`), so a crawl keeps going if the app closes and is found again when it reopens (`cache/jobs/running.json`). It shows matches per tier for the current patch. Updates wait until a job ends.

Each job is also a subcommand of the app (`draftassist crawl --hours 8`, or `draftassist-cli crawl --hours 8` when installed): crawl, fit, learn, split, postgame, import-games, ddragon, opgg, coverage, power, backfill, pools, scout-check, and for storage and shared data cache-pack, cache-archive, db-optimize, publish, pull, shared-inspect, account, members (`src/draftassist/commands.py`). A user (signed in with an invite) can't run crawl, fit, power, backfill, coverage, publish or members (exit 2; the Crawl page hides them). `scout-check` reports which enemy identity fields the recorded champ selects carry, per queue (`docs/enemy-scouting.md`).

**Where files live.** A source checkout keeps everything in itself, as below. The installed app (PyInstaller, `config.is_frozen()`) reads its bundled files from the bundle (`config.RESOURCE_ROOT`: `web/dist`, `data/archetype_matchups.yaml`, sample fixtures) and writes to `%LOCALAPPDATA%\DraftAssistant` (`config.DATA_ROOT`: the databases, `cache/`, `.env`, recordings, `data/roster.json`, and the tags and rules YAML, seeded from the bundle on first run and never overwritten). `DRAFTASSIST_HOME` overrides the data folder. `draftassist import-data <checkout>` copies a checkout's databases, `.env` and edited YAML into it (`--with-cache` for `cache/`; `--user` keeps only a teammate's own data: crawl tables, ladder seeds and publishing settings are dropped, because they come from Turso).
Since `docs/shared-data.md`: the Riot cache is one `cache/cache.sqlite` (zstd with a trained dictionary; old `.json` files are still read until `cache-pack`); `account.json` in the data folder holds the role, name and database URL (nothing secret); the tokens are in Credential Manager (service `DraftAssistant`, one entry per secret and data folder), with `credentials.json` in the data folder only where no keyring works. `import-data` copies neither.

| What | Command |
| --- | --- |
| Champion names/icons (each patch) | `uv run python -m draftassist.data.ddragon` |
| OP.GG bootstrap stats (each patch) | `uv run python -m draftassist.data.opgg` |
| Crawl (overnight; resumable) | `uv run python -m draftassist.data.crawl --hours 8` (~1,800 matches/h) |
| Crawl an older patch, gold leads first (resumable) | `uv run python -m draftassist.data.crawl --patch 16.21 --ids-per-queue 100 --timelines-first --hours 8` |
| Crawl Master+ for the Matchup tree only (own database, `data/master.sqlite`; resumable) | `uv run python -m draftassist.data.crawl --master --ids-per-queue 100 --hours 12` |
| Fetch timelines again for the newer stats, then stop (prints its cost first; resumable; add `--master` for that database) | `uv run python -m draftassist.data.crawl --timelines-refetch --share 1` |
| Fit the expectations and state model (a few minutes; after a refetch or a big crawl) | `uv run python -m draftassist.data.execution_model` |
| Crawl coverage / Phase 3 acceptance | `uv run python -m draftassist.data.coverage` |
| Learning job (link players, history, comfort) | `uv run python -m draftassist.learning.jobs` |
| Confirm logged games via match-v5, and store their timelines | `uv run python -m draftassist.data.postgame` |
| Import games History missed (3+ of us together, last 60 days) | `uv run python -m draftassist.data.import_games` |
| Import pools from a file | `uv run python -m draftassist.data.pools import pools.txt` |
| Ban value breakdown (audit) | `uv run python scripts/ban_audit.py [--real]` |
| Damage, game length, trait sources, runes and items for already-crawled matches (from the Riot cache, no API calls) | `uv run python -m draftassist.data.backfill` |
| Lane power curves by milestone (Strategy tab; after a crawl; downloads Meraki's data weekly) | `uv run python -m draftassist.data.power` |
| What the crawl says a damage-heavy team is (Strategy view), and how many matches have builds | `uv run python -m draftassist.data.builds` |
| App without the client | `draftassist --demo` or `draftassist --replay fixtures/lcu/flex-ranked-sample` |
| Pack the Riot cache's `.json` files into `cache/cache.sqlite` (verifies every row before deleting a file; resumable; `--keep` keeps the files) | `draftassist cache-pack` |
| Move old patches' cached matches into `cache/cache-archive-<patch>.sqlite` (still read) | `draftassist cache-archive --older-than-patches 6` |
| Smaller databases: measure on a scratch copy, then apply (WITHOUT ROWID, unused indexes, VACUUM; refuses while a job runs) | `draftassist db-optimize --dry-run`, then `draftassist db-optimize` |
| Publish the counts and fits to Turso (admin; changed blocks only; fits the datasets first; resumable) | `draftassist publish [--dry-run]` (or Publish on the Crawl page) |
| Pull the shared data (users; the app also does it at start, every 3 h and on Sync now) | `draftassist pull` |
| What is published, decoded (Turso's console only shows blocks) | `draftassist shared-inspect [--table cnt_role --patch 16.22]` |
| Sign in or out | `draftassist account status`, `account join <code>`, `account admin --url ... --org ... --database ... --name ...` (asks for the tokens), `account sign-out` |
| The team's members (admin) | `draftassist members list`, `add NAME`, `renew NAME`, `remove NAME --yes` (rotates keys: everyone else gets a new code) |
| Crawl only some Master+ ladders (own database) | `draftassist crawl --master --apex-tiers challenger grandmaster --hours 4` |

Env vars: `DRAFTASSIST_LCU` (client | demo | replay | off), `DRAFTASSIST_REPLAY`, `DRAFTASSIST_DB` (use a scratch copy for experiments), `DRAFTASSIST_RECORD`, `DRAFTASSIST_DEMO_LOG`, `DRAFTASSIST_STATS_REFRESH_S`, `DRAFT_FORMAT` (auto | tournament | ranked).

## Team context
- Ranked 5s uses tournament draft (3 bans, 3 picks, 2 bans, 2 picks); Flex/Solo use standard draft (5 simultaneous bans each, then B RR BB RR BB R). Both are supported and auto-detected.
- Stats bracket is set per player on the Pools page (currently Top Gold, Jungle Emerald, Mid Gold, ADC Platinum, Support Iron). Enemy prior = Gold. The crawl covers every tier Iron–Emerald.
- Weights live in Settings (stored in the DB, `app_settings`): α (meta) = 1.5, β (comfort) = 0.3, comfort games cap = 10, ban weights Threat 1.0 / Matchup 1.5 / Comp 1.0 / self-ban penalty 1.0, synergy v2 weights Stat 0.50 / Style 0.25 / Team 0.25 (the team model's damage rule replaced the 85% damage penalty), comp coherence on (0.15, towards an identity), ban Comp synergy off, synergy bracket "all". The spec's defaults (α = β = 1) were changed on purpose.

## Status (2 Nov 2026)
| Phase | State |
| --- | --- |
| 0 Skeleton, 1 Engine | Done |
| 2 Live LCU + UI | Done; checked in a real lobby |
| 3 Data pipeline | Built; acceptance pending: the crawl needs ~20k matches per tier (~40 h on a dev key); run `data.coverage` |
| 4 Pools + logging | Done; real Flex games appear in History |
| 5 Learning | Done (comfort re-scoring with accept/lock, draft report, team style, prep sheet) |
| 6 Model + scouting | Scouting built (`docs/enemy-scouting.md`, Stages A–D, 7 Nov 2026); the learned model not started |

Extra task docs done: `ban-score.md`, `ban-guardrails.md`, `flex-simultaneous-bans.md` (group bans: at most 2 per enemy lane, decided 8 Nov 2026), `ban-value-matchups.md` (audit in `ban-value-audit.md`), `history-broadcast-ui.md`. `synergy-visualizer.md` built (Stages A–C; its Synergy page is now part of Prep, see `synergy-v2.md`): role-aware `synergy_pairs` + damage profile, engine synergy term from `EngineData.synergy`, damage balance, "Next 2 picks". Pairs, traits, fits, damage and timing pool every crawled patch up to the current one (`data/synergy.crawl_weights`, weight halves every 6 patches); win rates and lane matchups are current patch only. `comp-style.md`: Stages A–B done (game length, power curves, style seed + Champions review page (both since replaced by the tags, see `synergy-v2.md`), Early/Late threshold ±0.15; comp panel for both teams, badges on Live / Synergy / Pools, 5 / 3 suggestion cards). Stage C built, awaiting review: comp coherence in pick value (not P(win)), damped 0 / 0.1 / 0.25 / 1 / 1 by our pick number, target from the Team page else our comp; Team page on the same labels; varied Next 2 picks (`pair_repeat_margin`). The old role-less `synergy` table is gone (`db/session.DROPPED_TABLES` drops it from older databases).

`synergy-v2.md` (supersedes the visualiser's page and engine term; keeps its Stage A pair score as **Stat**), merged to `main` on 3 Nov 2026 (Stages 1–6 plus the follow-ups below; decision log in the doc, Stages 3–6 built without step-by-step review): measured traits (`champion_traits`, percentiles per role over the synergy window; field mapping in `data/traits_map.py`), judgement tags in `data/champion_tags.yaml` (drafted by Claude, unreviewed = half weight) with a Tags page (`#/tags`); style rules (`data/style_rules.yaml`, ridge fit of pair Stat residuals) and team rules (`data/team_rules.yaml`, logistic fit on crawled matches), stored in `synergy_fits` by every aggregation, with a Rules page (`#/rules`); Stat / Style / Team evaluator (`engine/synergy_v2.py`, `POST /api/synergy/evaluate`); the Synergy page merged into Prep (visualiser, plan panel, synergy table; `api/prep_view.py`, `web/src/PlanSynergy.svelte`); plan-driven bans (Win lane / Win teamfights / Combined) and picks (order by side, fills, risks, backups) in `engine/plan.py`, `POST /api/prep/plan`, `web/src/PlanBansPicks.svelte`; "Pin for next game" (`/api/plan/pin`, `app_settings.pinned_plan`): Live plan bar at the bottom (status per planned champion, plan vs off-plan bans, plan vs now damage / timing / style), plan ban and pick bonuses (0.3 log-odds, Settings), `drafts.plan_json` and plan adherence in History. Since review: **Live scores synergy v2** (`EngineData.synergy_v2`: pairs' Stat + Style and w_team × Team vs enemy, batched; the 85% damage penalty is gone), and **fight style is one vocabulary**: the five identities (Engage / Poke / Pick / Protect / Split) from the tags, used by badges, comp panels, coherence and the Team page; the comp-style seed and its review page are retired (the Tags page is opened from Settings, Champion data, since 8 Nov 2026; no longer a nav tab). After pulling: run `data.ddragon` (attack range), then `data.backfill` (trait sources; it re-aggregates).

`matchup-archetypes.md` (Stages 1-7, built 4 Nov 2026; decisions and measured findings in the doc): an archetype per champion and role and kit tags (`data/champion_tags.yaml`, three review flags, `#/tags`); matchups shrink towards strength + archetype + kit (`data/matchup_fit.py`, `matchup_fits`) instead of 50%, and `BracketStats.matchup_edge` removes strength so P(win) doesn't count it twice; the lane matchup is the mean gold lead at 15 minutes (`data/timelines.py`), blended with the game matchup for ban answers, counter risk and History (Settings); fitted role weights; bot lane 2v2 (`engine/bot_lane.py`); blind safety, comfort gate, `has_fight_starter`, check strip, pool health (`engine/blind.py`); Matchup tree on Prep: up to 3 answers per archetype (the pool's best if it covers it, the rest from outside the pool; chips and gaps are about the pool alone), one of which may be a tagged counter (`engine/matchup_tree.COUNTER_PP`), with its own patch weighting (`tree_patch_half_life`, default 0 = every game counts the same; observed matchups and lane leads only, not the fits or the pool health line). The tree reads Master+ from `data/master.py` once that database has 2,000 matches: its own database because everything else pools every tier of the main one. Only the pool health line stays on the main data. After pulling: restart the app (it syncs the tags), and run `data.crawl --timelines-first` until the backlog clears.

`draft-vs-execution.md` (stages reviewed one at a time; decisions in the doc): Stage 1 built 5 Nov 2026. Everything a timeline gives is read in one pass (`data/timeline_map.py`; stored by `data/timelines.py`) onto `match_participants` and `match_team_state`, with `timeline_version` marking what's complete. Our own games go to `own_game_participants`, never `match_participants`, because aggregation reads that. `data.postgame` fetches their timelines. Stage 2 built 6 Nov 2026: expectations with residual SDs and the state model (`engine/execution.py`, `data/execution_model.py`, stored in `execution_fits`; a batch job, not part of aggregation). Raw draft log-odds are about five times too sure of themselves (the doc's decision 17), so Live's lock-in screen shows the calibrated P(win) (decision 32; `load_draft_scale` in `api/main.py`), with the raw figure beside it. Stage 3 built 6 Nov 2026: `learning/execution.py` splits each logged game into draft, early and conversion, with role shares, labels and a verdict, in `game_execution` and `game_execution_roles`. The fit recomputes every game; the learning job and verification fill in new ones. Stage 4 built and checked in a browser on 6 Nov 2026 (synthetic rows): the History strip (`web/src/ExecutionStrip.svelte`, `execution_payload` in `api/recap.py`) and a verdict chip per game. Stage 5 built 6 Nov 2026: trends on the Team page (`learning/trends.py`, `GET /api/team/trends?window=10|20|all`), read-only over Stage 3's tables. Stage 6 built 6 Nov 2026, off by default: a lane hint on the Pools page (Settings checkbox; `learning/execution_comfort.py`, decision 35). The open decisions were answered (decision 36). See `docs/handoff-draft-vs-execution.md`.

`enemy-scouting.md` (Stages A–C, built 7 Nov 2026): Live scouts the enemy players when the client gives their Riot IDs (Ranked 5s), or from IDs pasted in the Scout opponents box. `data/scout.py` fetches mastery then up to 20 ranked games each (`scout_players`, `scout_games`); `engine/scouting.py` blends each player's pool into the chance their player is on X (Threat, Matchup, likely enemy picks), and ban cards split into their pools' best and the rest's (5 + 2, then 3 + 1). Settings: on/off and the weight. Never in demo mode. Follow-up in the doc's decision 12. Stage E (7 Nov 2026,
decisions 13–17): mastery always a recency-weighted prior, joint role guessing (mastery-only players placed) with a
certainty that scales the blend, banned mains renormalised in the ban tables, failed fetches retried once, pasted IDs
fill gaps the client leaves.

`scouting-research.md` (8 Nov 2026): scouting is **Ranked 5s only** (tournament draft): their last 30 ranked games,
weighted by patch (`patch_weight`), each match read once for the whole team, all mastery and their rank, a running
crawl pauses while a 5s champ select scouts (`cache/jobs/yield.json`), and each draft records how scouting went
(`drafts.scout_json`; History chip and Overview line). Crawl page: live progress (`cache/jobs/progress.json`), crawl
options (patch, tiers, ids, timelines), a tier table that follows the running crawl (`crawl-page.md`). Strategy's
damage-heavy thresholds come from the crawl (`data/builds.damage_thresholds`).

`top-lane-power.md` (v0.2.x, from the user's research in `top-lane-power-research.md`): Stages 1–3 built. Strategy tab:
OP.GG's champion analysis for the player's tier and the lane matchup guide (`data/opgg_builds.py`, background fetch,
day cache in `cache/opgg/builds/`), and our own builds in order (`match_build_events`, kept from the timelines the
crawl already fetches; `data/builds.summarize_order`). Stages 4–5 built: lane power by milestone (lvl 2, 3, 6, item 1, ...;
19–20 for top), theory from Meraki's kits (`data/meraki.py`, `engine/power.py`) combined with gold leads measured from
per-minute frames (`match_frames`, every lane) by the **Lane power** job (`data/power.py`, `power_curves`); drawn on the
Strategy tab. Run it after a crawl.

`player-roster.md` (built 7 Nov 2026): one active player per role (`players.active`) and a bench that keeps a swapped-out
player's pool; "Swap in a sub" on the Pools page (`POST /api/players/swap`), Forget for benched players. A new Riot ID for
a linked player benches them; an unlinked one is renamed. Players and pools are mirrored to `data/roster.json` in the
data folder after every change (`data/roster.py`, no PUUIDs, gitignored) and restored from it into an empty database.

`import-games.md` (built 8 Nov 2026): **Import missed games** (History page; Crawl job `import`; `draftassist
import-games`): our active players' ranked games of the last 60 days with 3+ of them on one team that History doesn't
have become drafts from match-v5 (`data/import_games.py`, `drafts.imported`; pick order is the participants' order, no
suggestions), with their timelines.

`ui-engine-review.md` (8 Nov 2026; decisions in the doc; built without step-by-step review, as asked): Tags and Rules
open from Settings (Champion data), not the top bar. **Live speed and no-crash** (`live-speed.md`): the connector
`submit`s sessions to one builder thread (newest wins), board first after a slow build, each panel fails on its own
(`panel_errors`), the last good suggestions stay up marked stale, `build_ms` on every payload. **Enemy pools and lanes**
(`enemy-lanes.md`): role chances for flex picks (`their_role_probs`), the likely picks per open enemy slot
(`likely_by_role`), one-trick / mastery-only / role-certainty labels, whose champion a ban takes. **History charts**
(`history-charts.md`): an Overview band (Apache ECharts, lazily loaded; `GET /api/history/stats`; a timeframe picker, last 10 / 20 / 50 /
100 / all, sends `?last=N`, and the rolling line uses 5 games at 20 or fewer). **Strategy tab** on
Live (`strategy.md`): runes, spells and items from `match_builds` (stored by the crawl; `data.backfill` fills older
matches from the cache) with names from Data Dragon, what Master+ runs and how it wins in the player's bracket, lane
plan, game plan and threats (`engine/strategy.py`, `data/builds.py`, `api/strategy.py`). **Top bar** Live · Prep ·
Review (History + Team) · Roster (Pools) with Crawl and Settings as icons, and Live polish (`nav-and-live-polish.md`).
After pulling: `uv sync`, `cd web; npm ci; npm run build`, then `data.ddragon` (item and rune names) and `data.backfill`
(runes and items for matches already crawled).

`shared-data.md` (Stages 0–6, built 8 Nov 2026; the measurements, notes and the decision log are in `shared-data-stage0.md` and the task doc): storage (packed zstd cache, WITHOUT ROWID databases: 82 → 48 MB), per-(patch, tier, queue) count tables (`data/counts.py`) from which every machine derives the runtime tables the same way, publish/pull of compressed blocks over Turso's HTTP API (`data/shared/`), invite sign-in with admin/user/solo roles and members (rotation re-issues everyone's codes), datasets with their own fits and each user's data selection (`data/datasets.py`), the installer's Join / Team admin / Solo choice. Strategy reads the counts and shows "Helpful in this lane" hints instead of a vs-opponent build (decision 6). The Crawl page picks tiers in sets (Emerald and below, Above Emerald, Master+) and shows matches the same way. Its **Storage** section (`data/storage.py`, `api/storage_routes.py`) shows what takes space in the data folder, clears downloaded installers and backups, and runs Pack cache, Archive old patches and Optimise databases as jobs; raw crawl matches are never deleted.

Open points:
- Ranked 5s queue id and what `theirTeam` shows in Ranked 5s: unknown until the first recorded Ranked 5s champ select (recordings land in `fixtures/lcu/recorded/`).

## Code map (`src/draftassist/`)
- `engine/strategy.py`: Live's Strategy view (lane plan, build reasons, win conditions); `data/builds.py` reads
  `match_builds` and Data Dragon names; `api/strategy.py` caches per champion, role and opponent; `api/history_stats.py`
  is History's Overview band.
- `engine/`: pure scoring. `engine.py` (scores, picks, bans = rel × discount × (a·Threat + b·Matchup + c·Comp) − d·OwnLoss, Flex group bans; synergy term from `synergy_v2.py` when attached; read-only views for planners), `lane_ban.py`, `matchup_model.py` (matchups smoothed by archetype and kit), `bot_lane.py` (2v2), `blind.py` (blind safety, pool health), `matchup_tree.py`, `comp.py` (timing, the five identities, badges, team comp), `traits.py` / `style.py` / `team.py` / `synergy_v2.py` (synergy v2 traits, pair style rules, team model, Stat / Style / Team evaluator), `plan.py` (plan bans and picks, pin), `guards.py` (Rules 1–3, last step before any list leaves the engine), `formats.py`, `state.py` (`DraftState.unavailable()` is the only availability source), `mathx.py` (the one logit / sigmoid).
- `lcu/`: `connector.py` (ReadOnlyLcu, GET only), `parser.py` (all LCU field names; `parse_session` = scan actions, fill steps, slots), `recorder.py`, `demo.py`.
- `api/`: `main.py` (Services, build_services, demo / replay / stats loops, app assembly), routers: `pool_routes.py` (players, pools, import, tier accept / lock), `settings_routes.py`, `learning_routes.py` (lane ban tables, learning job, Team style, prep sheet, model check), `history_routes.py` (history, recap, art), `synergy_routes.py` (synergy v2: Prep synergy and plan, pin, tags, rules, evaluate, synergy settings); `prep_view.py` / `synergy_v2.py` (synergy JSON), `deps.py` (`db_of`), `live.py` (Live view, plan bar, WebSocket hub), `draftlog.py`, `recap.py` (History).
- `data/`: `pools.py` (players, bench, swaps, pools) + `roster.py` (the roster file), `archetypes.py`, `matchup_fit.py` (fits) + `matchup_model.py` (reads them), `timelines.py` (docs/matchup-archetypes.md), Riot client/crawler/aggregate, synergy v2 (`traits_map.py`, `traits.py`, `tags.py`, `rules.py`, `synergy_model.py`), OP.GG bootstrap, provider (OP.GG blended with crawl per champion), pools/players, settings store, postgame.
- `learning/`: comfort, history, jobs, report, style, prep, execution (per-game split), trends (Team page trends), execution_comfort (Pools page lane hint).
- Scouting: `data/scout.py` (fetching, pools, role assignment, `scout-check`), `engine/scouting.py` (the blend, `ScoutedLaneData`), `api/scout_routes.py`.
- The app around the engine: `launcher.py` (window or browser, subcommands), `window_chrome.py` (title bar, icon, remembered size), `config.py` (`RESOURCE_ROOT` / `DATA_ROOT`), `commands.py` (jobs as subcommands, `import-data`), `jobs.py` + `api/jobs_routes.py` (Crawl page), `updates.py` + `first_run.py` + `api/setup_routes.py` (updates, first run, Riot key), `packaging/` (installer).

## Lessons learned (don't relearn these)
- Turso (8 Nov 2026): blobs come back as base64 **without** `=` padding; a token minted straight after a key rotation is signed with the retiring key and stays dead (one minted a minute later works), so after a rotation every new token is checked and minted again; Turso's console only shows the blocks (`draftassist shared-inspect` decodes them).
- The admin's per-match rows must stay on the admin's machine: counts, traits, the execution fit, power curves and build backfills are all computed from them. Publishing copies counts and fits out; it frees nothing locally.
- The count tables are keyed (patch, bracket, queue, ...): read them through the slices in `cnt_bracket` with a `CROSS JOIN`, or SQLite scans a whole patch per lookup (`tests/test_db_optimize.py` checks the plans). WITHOUT ROWID makes secondary indexes on text keys a little bigger (they hold the key instead of a rowid); the database still shrinks by about 40%.
- Secrets never go on a command line (other programs can read it): the CLI asks with a hidden prompt, and the installer hands them over in a file in its temp folder that the app deletes as soon as it has read it.
- The League client's PUUID (a plain UUID) is not the Riot API's (encrypted per key). Players keep both: `client_puuid` for champ select, `api_puuid` for match-v5 (from account-v1).
- LCU: a skipped ban is championId `-1` (0 = not chosen yet). Standard-draft bans are one simultaneous turn and the enemy's stay hidden until `ten_bans_reveal`. The two picks of a turn can lock in either order. `myTeam[].championId` is also set while hovering: locked picks come from completed actions.
- `theirTeam` in Flex has no names, PUUIDs or positions: no enemy scouting there.
- Enemy roles must be the most likely assignment (log role share from pick rates), never the one maximising their win rates: off-role games are rare and noisy (Graves top: 0.03% picks at 51% WR) and put junglers top.
- SQLite runs in WAL mode (the app reads while the crawl writes). WAL files must never be committed.
- `index.html` is served `no-cache`, otherwise browsers keep running an old UI bundle after updates.
- `uvicorn[standard]` is required: plain uvicorn 404s the WebSocket.
- Colour identity (9 Nov 2026): midnight blue surfaces, an analogous azure for quiet accents, bright yellow `--gold` only for highlights and main actions (`button.primary` / submit). Tokens in `app.css`, the same values for canvas charts in `web/src/palette.js`. Chart colours were validated with the dataviz palette checker on the panel surface `#101a30`: favourability teal `#1baf7a` / orange `#e0702e` (amber read as a highlight next to the yellow; red/green fails colour-blind checks), component bars blue/orange/aqua, sides `#4a8ee0` / `#d0574e`, the five identities unchanged.
- Motion lives in `web/src/motion.js` (entrances ease out, movement eases in-out, exits 20% shorter, 30 ms row stagger, mask reveals, arcs on the synergy ring, one shared key for the nav underline). Pages go one at a time: the old page fades to blank (160 ms) while the new one mounts out of sight (`web/src/loading.js`), and the new one rises in once what it started has arrived (its requests, imports passed to `track()`, its images; at most 900 ms) and the fade is done. So a page's first request must go out at once: debounce only later edits (`PlanBansPicks`, `PlanSynergy`). Live is in the main bundle; every other page is its own chunk (`App.svelte` `LOADERS`), fetched on first open, on hovering its tab, or once the app is idle. Hashed files under `assets/` are served `immutable` (`api/main.UIFiles`).
- Development Riot keys expire 24 h after they are generated; the crawl stops with a clear message and resumes after a new key is pasted into `.env`. Since 2 Nov 2026 `.env` holds an approved app key: no expiry, but the same 20 / 1 s and 100 / 2 min limits (~1,800 matches/h).
- Riot encrypts PUUIDs per app: PUUIDs fetched with one key get 400 from another. Cache entries carry a key fingerprint (`riot.key_id`, a one-way hash) and `riot.sync_api_key` drops ladder seeds and players' `api_puuid` when the key changes (the crawl re-seeds; the learning job re-resolves Riot IDs). The client's PUUIDs are unaffected.
- `data.ddragon` sets the current patch for everything. On a new patch, Live's win rates and matchups fall back to OP.GG until the crawl catches up, and the crawl only keeps the new patch's matches. So run it deliberately, stop the crawl first, and follow it with `data.opgg`.
- Modules imported inside functions load when first called: a long-running crawl picks up whatever is on disk at its first aggregation (15 min in). Preload `data.synergy_model`, `data.tags`, `data.profiles` and friends before editing code during a crawl, or restart it after.
- `challenges.laningPhaseGoldExpAdvantage` is only ever 0 or 1, not a gold figure. Real leads come from match-v5 timelines (about 800 KB each, so not cached): `data/timelines.py`. One rate limiter per process: run them inside the crawl, not beside it.
- Players' patch 26.22 is Riot's 16.22: match `gameVersion` and Data Dragon both say 16.x. Stored patches, queries, filters and art URLs keep Riot's number; only what a person reads changes (`web/src/patch.js`, `data/crawler.display_patch`). Docs and logs below say 16.x.
- Timeline facts (20 real games, Nov 2026): `teamId` on a plate or building is the team that lost it. 39% of `WARD_PLACED` events are type `UNDEFINED`: ability objects from Yorick, Shen, Jhin and others, not vision. A third of plates have `killerId` 0. Plates now fall well past 14:00.
- Don't call Riot directly while a crawl has just stopped: the 2-minute window is still full. Use `RiotClient`, which paces itself and waits on 429s.
- Playwright MCP saves screenshots relative to the workspace root (`Draft/`), not this project: pass `lol-draft-assistant/tests/screenshots/...`. It also writes page snapshots (with player names) to `.playwright-mcp/`; delete it after a session. Replay mode logs drafts: point `DRAFTASSIST_DB` at a scratch copy first.
- When editing files that contain non-ASCII characters (⚠, −, ·), use the Edit/Write tools; shell heredocs and nested escaping have broken these several times.
