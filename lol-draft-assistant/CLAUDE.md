# LoL Draft Assistant (Ranked 5s)

Personal, local pick/ban assistant for one 5-stack on EUW. Read `docs/SPEC.md` before doing anything — it is the source of truth for scope, architecture, data model and the phased build plan.

## How to work
- Build **one phase at a time** from "Build plan for Claude Code" in the spec. Stop at the end of each phase, show the acceptance check passing, and wait for me before starting the next.
- Write `pytest` tests alongside the code; run them before saying a phase is done.
- `reference/draft_engine.py` is a working prototype of the v1 engine. Port its logic into `src/draftassist/engine/`; don't import from `reference/`.
- Ask before adding dependencies outside the spec's stack table, or before changing scope.

## Hard rules
- **Read-only League client.** The LCU connector may only GET and subscribe to events. Never PATCH/POST/PUT/DELETE to the client — no auto-hover, pick, lock, or accept.
- Secrets live in `.env` (`RIOT_API_KEY`). Never commit `.env`, never print the key.
- Respect Riot API rate limits: one shared rate limiter, cache every response on disk.

## Environment
- Windows (the League client only runs there). Python 3.12 managed with `uv`.
- Run tests: `uv run pytest`
- Run the app (once Phase 2 exists): `uv run uvicorn draftassist.api.main:app --reload`, then open http://localhost:8000

## Team context
- Ranked 5s uses tournament draft (3 bans, 3 picks, 2 bans, 2 picks). Keep the draft format configurable so Flex/Clash also work.
- Stats bracket per role: Top/Mid/ADC = Gold, Jungle = Emerald, Support = Iron. Enemy prior = Gold.
- Recommendations weight comfort and meta equally (α = β = 1, configurable).
