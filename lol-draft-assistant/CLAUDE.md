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

## Environment
- Windows (the League client only runs there). Python 3.12 managed with `uv`; web UI is Vite + Svelte 5 in `web/` (FastAPI serves `web/dist`).
- Run tests: `uv run pytest` (~720 tests, ~45 s; the fuzz test is the slow part). Tests run with `DRAFTASSIST_STRICT=1`, so suggestion guard violations raise.
- Run the app: double-click **Draft Assistant** on the Desktop (`.venv\Scripts\draftassist.exe`: starts the server, opens http://localhost:8000). Dev: `uv run uvicorn draftassist.api.main:app --reload`.
- After changing the UI: `cd web; npm run build`. After changing Python: restart the app (stats refresh live, code doesn't).

### Commands
| What | Command |
| --- | --- |
| Champion names/icons (each patch) | `uv run python -m draftassist.data.ddragon` |
| OP.GG bootstrap stats (each patch) | `uv run python -m draftassist.data.opgg` |
| Crawl (overnight; resumable) | `uv run python -m draftassist.data.crawl --hours 8` |
| Crawl coverage / Phase 3 acceptance | `uv run python -m draftassist.data.coverage` |
| Learning job (link players, history, comfort) | `uv run python -m draftassist.learning.jobs` |
| Confirm logged games via match-v5 | `uv run python -m draftassist.data.postgame` |
| Import pools from a file | `uv run python -m draftassist.data.pools import pools.txt` |
| Ban value breakdown (audit) | `uv run python scripts/ban_audit.py [--real]` |
| App without the client | `draftassist --demo` or `draftassist --replay fixtures/lcu/flex-ranked-sample` |

Env vars: `DRAFTASSIST_LCU` (client | demo | replay | off), `DRAFTASSIST_REPLAY`, `DRAFTASSIST_DB` (use a scratch copy for experiments), `DRAFTASSIST_RECORD`, `DRAFTASSIST_DEMO_LOG`, `DRAFTASSIST_STATS_REFRESH_S`, `DRAFT_FORMAT` (auto | tournament | ranked).

## Team context
- Ranked 5s uses tournament draft (3 bans, 3 picks, 2 bans, 2 picks); Flex/Solo use standard draft (5 simultaneous bans each, then B RR BB RR BB R). Both are supported and auto-detected.
- Stats bracket is set per player on the Pools page (currently Top Gold, Jungle Emerald, Mid Gold, ADC Platinum, Support Iron). Enemy prior = Gold. The crawl covers every tier Iron–Emerald.
- Weights live in Settings (stored in the DB, `app_settings`): α (meta) = 1.5, β (comfort) = 0.3, comfort games cap = 10, ban weights Threat 1.0 / Matchup 1.5 / Comp 1.0 / self-ban penalty 1.0. The spec's defaults (α = β = 1) were changed on purpose.

## Status (2 Nov 2026)
| Phase | State |
| --- | --- |
| 0 Skeleton, 1 Engine | Done |
| 2 Live LCU + UI | Done; checked in a real lobby |
| 3 Data pipeline | Built; acceptance pending: the crawl needs ~20k matches per tier (~40 h on a dev key); run `data.coverage` |
| 4 Pools + logging | Done; real Flex games appear in History |
| 5 Learning | Done (comfort re-scoring with accept/lock, draft report, team style, prep sheet) |
| 6 Model + scouting | Not started |

Extra task docs done: `ban-score.md`, `ban-guardrails.md`, `flex-simultaneous-bans.md`, `ban-value-matchups.md` (audit in `ban-value-audit.md`), `history-broadcast-ui.md`. Next up, not started: `synergy-visualizer.md`.

Open points:
- `flex-simultaneous-bans.md` test 2 assumes greedy recomputation spreads bans across lanes; under the lane ban score it concentrates (banning both bad top matchups is worth more). Implemented as specified; a per-lane diminishing-returns rule would need a decision.
- Ranked 5s queue id and what `theirTeam` shows in Ranked 5s: unknown until the first recorded Ranked 5s champ select (recordings land in `fixtures/lcu/recorded/`).

## Code map (`src/draftassist/`)
- `engine/`: pure scoring. `engine.py` (scores, picks, bans = rel × discount × (a·Threat + b·Matchup + c·Comp) − d·OwnLoss, Flex group bans), `lane_ban.py`, `guards.py` (Rules 1–3, last step before any list leaves the engine), `formats.py`, `state.py` (`DraftState.unavailable()` is the only availability source).
- `lcu/`: `connector.py` (ReadOnlyLcu, GET only), `parser.py` (all LCU field names), `recorder.py`, `demo.py`.
- `api/`: `main.py` (app, routes, Services, live stats refresh), `live.py` (Live view + WebSocket hub), `draftlog.py`, `recap.py` (History).
- `data/`: Riot client/crawler/aggregate, OP.GG bootstrap, provider (OP.GG blended with crawl per champion), pools/players, settings store, postgame.
- `learning/`: comfort, history, jobs, report, style, prep.

## Lessons learned (don't relearn these)
- The League client's PUUID (a plain UUID) is not the Riot API's (encrypted per key). Players keep both: `client_puuid` for champ select, `api_puuid` for match-v5 (from account-v1).
- LCU: a skipped ban is championId `-1` (0 = not chosen yet). Standard-draft bans are one simultaneous turn and the enemy's stay hidden until `ten_bans_reveal`. The two picks of a turn can lock in either order. `myTeam[].championId` is also set while hovering: locked picks come from completed actions.
- `theirTeam` in Flex has no names, PUUIDs or positions: no enemy scouting there.
- SQLite runs in WAL mode (the app reads while the crawl writes). WAL files must never be committed.
- `index.html` is served `no-cache`, otherwise browsers keep running an old UI bundle after updates.
- `uvicorn[standard]` is required: plain uvicorn 404s the WebSocket.
- Chart colours were validated with the dataviz palette checker on the panel surface `#161b22`: favourability teal `#1baf7a` / amber `#c98500` (red/green fails colour-blind checks), component bars blue/orange/aqua, sides `#2f6fb7` / `#c0423a`.
- Development Riot keys expire 24 h after they are generated; the crawl stops with a clear message and resumes after a new key is pasted into `.env`.
- When editing files that contain non-ASCII characters (⚠, −, ·), use the Edit/Write tools; shell heredocs and nested escaping have broken these several times.
