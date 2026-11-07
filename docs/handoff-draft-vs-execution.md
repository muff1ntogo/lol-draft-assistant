# Handoff: Draft vs execution, 6 Nov 2026 (after the cloud session)

Read `CLAUDE.md` and `docs/draft-vs-execution.md` first: the doc has the spec and decisions 1–36.

Merged to `main` on 6 Nov 2026 at the user's request, from branch `claude/eager-ramanujan-5zh36v` (which contains `claude/draft-vs-execution` plus the cloud session's commits). Everything was built on the session's recommendations; the user then answered the open decisions (decision 36), and Stage 6 was built with its flag off (decision 35). It still needs checking on the user's PC (below).

## Where things stand

| Work | State |
| --- | --- |
| Stages 1–3 | Built, tested, run on real data (earlier sessions) |
| Stage 4: the History strip | Built; looked at in a browser at 1920×1080 and 1000 px on synthetic rows; four fixes after the user's review (decision 28); the bars became number tiles (decision 34) |
| Stage 5: trends on the Team page | Built and tested; looked at in a browser on 16 synthetic games |
| Live shows calibrated P(win) | Built and tested; looked at in demo mode with a hand-made fit row |
| Stage 6: execution-aware comfort | Built and tested, off by default (decision 35); looked at in a browser on synthetic rows |

`data/execution_model.py` and `learning/execution.py` were not changed, so the fit running on the user's PC isn't affected. Live imports `DraftScorer` from `learning/execution.py` as it is.

## The user's decisions (6 Nov 2026)

1. **Stage 6:** build it now, flag off. Done (decision 35).
2. **Trends count only games with the whole split:** kept (decision 29).
3. **The lever tests only the most negative part:** kept (decision 30).
4. **Per player is per role, 10 games per player, 5 per champion:** kept (decision 31).
5. **Live: calibrated P(win) big, raw figure small, step-by-step figures raw:** kept (decision 32).
6. **Colours: teal / amber**, not red / green: kept (decision 28).

## What wasn't checked (needs the user's PC)

No real games, crawl data or fit exist in the cloud: `.env`, `data/*.sqlite*`, `cache/` and `tests/screenshots/` are never committed. So:
- The strip, trends, calibrated P(win) and the lane hint were only seen on synthetic rows and a hand-made scale of 0.21.
- The lane hint is off by default: tick it in Settings to see it on the Pools page.
- With 12 real games, the Team page will show "too few games for a trend" for windows of 10+ until 10 games have a timeline and a split.

## Next time on the user's PC

- Read `cache/execution-fit.log` and confirm Stage 2's numbers on all six tiers.
- Re-read the 12 real games: comfort's new scale changes P0, so verdicts will shift.
- Delete both `cache/*.ps1` scripts.
- Look again at `timing_gold` (decision 19).
- `uv sync` (pywebview is new), `cd web; npm run build`, then restart the app: the strip, trends and Live change need the new code.
- Lint: ruff ran once in the cloud (`uvx ruff check --isolated --select F`); it isn't a project dependency.

## The app window and installer (6 Nov 2026, after the above)

Three stages, each tested: the launcher opens a pywebview window (`draftassist-app.exe` has no console); the installed app
keeps its data in `%LOCALAPPDATA%\DraftAssistant` and runs the jobs as subcommands; `packaging/` builds the installer
(see `packaging/README.md`). Not yet seen on Windows by a person: the WebView2 window itself, and the installer's
shortcuts. To move to the installed app, stop the crawl and run `draftassist-cli import-data <this folder>` once.

## Rules the user set

- Make edits with Edit or Write, not scripts. Scripts are for read-only checks.
- Build in stages, plan first, tests first.
- Neutral wording: "ahead of / behind expectation", never "fault" or "blame".
- Display patches as players see them (26.22); stored keys stay 16.x.
- Commit or push only when asked. Ask before adding dependencies. Only one crawl at a time. Never write to the League client.
- Never commit raw timelines, PUUIDs, player names or the key.
