# Handoff: Draft vs execution, 6 Nov 2026 (after the cloud session)

Read `CLAUDE.md` and `docs/draft-vs-execution.md` first: the doc has the spec and decisions 1–33.

The cloud session's work is on branch `claude/eager-ramanujan-5zh36v`, which contains `claude/draft-vs-execution` plus the commits below. Neither is merged to `main`. The user asked for everything to be built on the session's own recommendations, with the open decisions gathered here for one final change before merging.

## Where things stand

| Work | State |
| --- | --- |
| Stages 1–3 | Built, tested, run on real data (earlier sessions) |
| Stage 4: the History strip | Built; looked at in a browser at 1920×1080 and 1000 px on synthetic rows; four fixes after the user's review (decision 28) |
| Stage 5: trends on the Team page | Built and tested; looked at in a browser on 16 synthetic games |
| Live shows calibrated P(win) | Built and tested; looked at in demo mode with a hand-made fit row |
| Stage 6: execution-aware comfort | Not built (decision 33) |

`data/execution_model.py` and `learning/execution.py` were not changed, so the fit running on the user's PC isn't affected. Live imports `DraftScorer` from `learning/execution.py` as it is.

## Decisions for the user (one final change before `main`)

1. **Stage 6.** Build it (behind its Settings flag, default off) or drop it for now? Recommendation: wait until there are 30+ logged games with timelines; with 12 the per-champion hint is noise.
2. **Trends count only games with the whole split** (decision 29). Alternative: count draft-only games for the Draft mean. Recommendation: keep, so the three means are over the same games.
3. **The lever tests only the most negative part** (decision 30). Alternative: the most negative part whose band excludes 0. Recommendation: keep, as the doc words it.
4. **"Per player" is per role, 10-game threshold for players, 5 for champions** (decision 31). Alternatives: group by the logged in-game name, or no player threshold.
5. **Live's lock-in shows calibrated P(win) big and the raw figure small** (decision 32). Alternatives: calibrated only, or also calibrate the step-by-step figures (suggestion cards, History's timeline). Recommendation: keep; step-by-step figures compare picks with each other, and the scale doesn't change their order.
6. **Colours:** teal / amber for good / bad everywhere (decision 28), not the red / green the user mentioned, because red / green failed the colour-blind check in `CLAUDE.md`. Say if red / green is wanted anyway.

## What wasn't checked (needs the user's PC)

No real games, crawl data or fit exist in the cloud: `.env`, `data/*.sqlite*`, `cache/` and `tests/screenshots/` are never committed. So:
- The strip, trends and calibrated P(win) were only seen on synthetic rows and a hand-made scale of 0.21.
- With 12 real games, the Team page will show "too few games for a trend" for windows of 10+ until 10 games have a timeline and a split.

## Next time on the user's PC

- Read `cache/execution-fit.log` and confirm Stage 2's numbers on all six tiers.
- Re-read the 12 real games: comfort's new scale changes P0, so verdicts will shift.
- Delete both `cache/*.ps1` scripts.
- Look again at `timing_gold` (decision 19).
- `cd web; npm run build`, then restart the app: the strip, trends and Live change need the new code.
- Lint: `ruff` isn't installed in either environment, so it hasn't run on this work.

## Rules the user set

- Make edits with Edit or Write, not scripts. Scripts are for read-only checks.
- Build in stages, plan first, tests first.
- Neutral wording: "ahead of / behind expectation", never "fault" or "blame".
- Display patches as players see them (26.22); stored keys stay 16.x.
- Commit or push only when asked. Ask before adding dependencies. Only one crawl at a time. Never write to the League client.
- Never commit raw timelines, PUUIDs, player names or the key.
