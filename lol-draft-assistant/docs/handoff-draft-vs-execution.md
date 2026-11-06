# Handoff: Draft vs execution, 6 Nov 2026

For the next session, which runs in the cloud. Read `CLAUDE.md` and `docs/draft-vs-execution.md` first: the doc has the spec and decisions 1–27.

Work is on branch `claude/draft-vs-execution`. It is not merged to `main`.

## Where things stand

| Work | State |
| --- | --- |
| Stages 1–3 (timeline stats, expected values and state model, per-game split) | Built, tested, and run on real data on a scratch copy |
| Ban Threat now follows real pick rate (`docs/ban-value-matchups.md`, "Later change") | Built, tested, checked on real data |
| Comfort takes the draft's scale in P0 (decision 26) | Built and tested. Not re-run on real games since the change |
| **Stage 4: the History strip** | **Backend tested, UI builds. Nobody has looked at it yet** |
| Stage 5: trends on the Team page | Not started |
| Live shows calibrated P(win) | Not started. Agreed as a small separate change after this feature |
| Stage 6: execution-aware comfort | Not started. **Ask the user before building it** |

`uv run pytest`: 1,233 passed on this branch. `ruff` isn't installed locally, so lint hasn't run.

## Next steps, in order

1. **Finish Stage 4's visual check.** The spec asks for a screenshot of the strip at 1920×1080 and at 1000 px wide. The session stopped just before this, so `web/src/ExecutionStrip.svelte` has never been rendered. Look for label collisions, overflow at 1000 px, the bot chip opening, the "Show every stat" table, the draft-only state, and the verdict chip in the game list. Then stop for the user's review.
2. **Stage 5**, as specified in the doc, tests first, then stop for review.
3. **Calibrated P(win) on Live**, as a small separate change. It uses `DraftScorer`'s formula: `sigmoid(scale × (draft log-odds + comfort))`.

## What the cloud session can't do

These live only on the user's PC and are never committed: `.env` (the Riot key), `data/*.sqlite*`, `cache/`, `tests/screenshots/`, `fixtures/lcu/recorded/`. So the cloud session has no real games, no crawl data, no fit and no League client.

- Tests run on synthetic data, so they work as usual. Run `uv sync`, and `npm install` in `web/` before building.
- For screenshots, use a scratch database (`DRAFTASSIST_DB`) with `DRAFTASSIST_LCU=demo` and `DRAFTASSIST_DEMO_LOG=1`, and insert `GameExecution` and `GameExecutionRole` rows by hand. `attributed()` in `tests/test_recap.py` shows the shape. Don't commit the screenshots.
- Anything needing real data has to wait for the user's machine. Say what you couldn't check, rather than implying you did.

## Still running on the user's PC

These don't depend on the cloud session and need nothing from it:

- **The refetch** (`cache/crawl-chain.ps1`, log `cache/refetch.log`) was at 10,884 of 19,531 at 03:15 on 6 Nov, at about 2,700 an hour, so it should finish around 06:30.
- **The full fit** (`cache/fit-after-refetch.ps1`) starts when the refetch ends, writes `cache/execution-fit.log`, then fills in `game_execution` for the logged games.

The fit reads the code on disk at that moment. If the user pulls or switches branches on that PC before it runs, a breaking change to `data/execution_model.py` or `learning/execution.py` would break it. Until the fit is done, keep those modules compatible or tell the user.

**Next time the session is back on that PC:**
- Read `cache/execution-fit.log` and confirm Stage 2's numbers on all six tiers. The earlier fits covered only part of the data, three tiers in the printed per-tier table.
- Re-read the 12 real games: comfort's new scale changes P0, so verdicts will shift.
- Delete both `cache/*.ps1` scripts.
- Look again at `timing_gold` (decision 19).
- Restart the app: the ban change and the strip need new code.

## Findings worth knowing

- **Latest fit (10,454 matches, 2,031 held out):** state Brier 0.166, draft-only 0.250, coin flip 0.250. Draft scale 0.21. Log-odds per unit: gold +0.366, towers −0.099, plates +0.034, dragons +0.302, grubs +0.032, herald 0, timing_gold −0.254.
- **The draft signal is small:** raw draft log-odds are about 5× too sure of themselves, and on real games the neutral draft stays within 43–53%.
- **Comfort is today's value** (decision 25), so a past game's P0 reads comfort learned partly from that game. Our data has only 12 games.
- **`deaths_while_ahead` looks high but is real:** 33 in one of our games, against a crawl mean of 6.0 and a 99th percentile of 30.
- **Ban list after the fix:** Irelia, Pyke, Kha'Zix, Darius, Aatrox, Talon, Sett, Ekko, Sion, Briar. Aurelion Sol fell from 4th to 91st. Leona has the top Threat but is 12th, because our pools handle her. Popular picks under 50% still rank low, on purpose.
- **Left alone on purpose:** the enemy pick estimate (`log(pick rate) + 10 × logit(win rate)`) still drives "likely enemy picks" and the "N% likely" text on ban reasons.

## Rules the user set

- Make edits with Edit or Write, **not Python scripts**. Scripts are for read-only checks. This was slipped on once, on one test file; don't repeat it.
- Build in stages, plan first, tests first, and stop after each stage for review.
- Neutral wording: "ahead of / behind expectation", never "fault" or "blame".
- Display patches as players see them (26.22). Stored keys stay 16.x: use `web/src/patch.js` or `display_patch`.
- Commit or push only when asked. End commit messages with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Ask before adding dependencies outside the spec's stack. Only one crawl at a time. Never write to the League client.
- Never commit raw timelines, PUUIDs, player names or the key.
