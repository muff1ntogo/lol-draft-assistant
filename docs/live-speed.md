# Live speed and no-crash (8 Nov 2026)

First task from `ui-engine-review.md` (decision 1). Built in one stage.

## What changed

- **Coalesced events.** The League client connector calls `LiveService.submit(session)`, which returns at once. One
  worker thread (`live-builder`) builds the newest waiting session. A session that a newer one replaces before its
  build starts is dropped (`LiveService.dropped` counts them): during a hover burst only the newest screen matters.
  Demo and replay modes still call `handle_session` directly, one event at a time.
- **One build at a time, in order.** `handle_session`, `view`, `refresh`, idle phases and status changes share one
  lock, so a slow build can't publish a champ select screen after the lobby screen. An idle phase (lobby, matchmaking)
  also drops any session still waiting.
- **Board first when slow.** If the last full build took over `BOARD_FIRST_MS` (150 ms), the next event first
  publishes the board (slots, bans, phase, timer) with the previous suggestions marked "Updating…", then the full
  screen.
- **Panels fail on their own.** `build_live_view` builds the board, then each panel through `_panel`: suggestions
  (the centre), hover scores, enemy slots (scouting), comp panels, plan and plan bar. A failure is logged, the panel
  is left out and named in `panel_errors`; Live shows "Not shown: …". Strict mode (tests) still raises.
- **Last good suggestions.** If the suggestions fail, the last good ones of the same draft stay up, faded, with
  "Suggestions couldn't update: showing the last good ones (from Ban 3)". Never across drafts (game id, and the
  draft must not have shrunk). Without a good one: "Suggestions are unavailable for this step". If even the board
  fails, the last screen stays up flagged. The draft log never records a stale centre as the step's suggestions.
- **Timing.** Every payload carries `build_ms`; builds over `SLOW_BUILD_MS` (250 ms) are logged with their phase.

## Tests

`tests/test_live_speed.py`: coalescing, idle phase drop, panel isolation, stale fallback (and not recorded by the
draft log), no stale centre across drafts, board first, and a budget (400 ms per event, strict) over every recorded
fixture and a simulated tournament and Flex draft.

## Not done

- Caching per-role meta weights and the scouting blend inside a champ select: with synthetic data a build takes
  2–26 ms, so there's nothing to win yet. Check `build_ms` / the log in a real champ select first.
