# UI/UX, History stats and engine review (8 Nov 2026)

A review after `import-games.md`. Proposals only, none built yet except **0**. Each one is sized as a possible task doc;
pick the ones you want and I'll write them up one stage at a time.

## 0. Done in this change: Tags out of the nav

- The **Champions** tab is gone from the top bar. **Settings → Champion data** links to the Tags page (`#/tags`) and
  the Rules page (`#/rules`). Prep's header keeps its Tags / Rules links.
- On those two pages the **Settings** tab stays highlighted and their back link says "← Settings".
- `#/champions` still opens Tags, so old bookmarks keep working.

## 1. UI/UX refinements

**Nav and layout**
- Seven tabs is still a lot for a five-stack between games. Group them by when you use them: **Live** · **Prep** ·
  **Review** (History + Team trends, as two sub-tabs) · **Roster** (Pools) · then **Crawl** and **Settings** as icons
  at the right. Team's trends really belong next to History.
- Keyboard shortcuts in champ select, where you have no time for the mouse: `1`–`5` expands a suggestion card,
  `Tab` switches between "our picks" and "their likely picks", `Esc` collapses everything.
- A compact Live mode (a setting, or automatic below ~1100 px wide) that drops the comp panels to a one-line strip,
  so the app fits beside the client on one monitor.

**Live**
- Show **why now**. Each card has a reason line, but not *what changes if we wait*. Something like "contested: 34%
  they take it next turn" or "safe to delay: nobody they're likely to pick counters it" turns a ranked list into a
  pick-order decision. The engine already has their likely picks (`enemy_turn` cards), so this is just a join.
- Confidence you can see: grey out or mark a card whose score rests on few games (the matchup sample, comfort games).
  Today a 3-game matchup and a 3,000-game one look the same.
- On a hover, show the delta against our #1 ("−2.1pp vs Orianna") rather than only its warnings.
- Colour the timer by urgency (gold under 10 s, red under 5 s). It's the one number that matters when time is short.

**General**
- Empty states that say what to do next ("No crawl data for 16.22 yet: Live is using OP.GG; start a crawl on the
  Crawl page") instead of a bare "—".
- One tooltip vocabulary: some pages say pp, some %, some log-odds (Settings). Say pp everywhere a person reads it, and
  show log-odds only in the advanced Settings fields, with the pp equivalent next to them (the plan bonus help already
  does this).

## 2. History page: stat display and graphs

Today History has the Model check (Brier + calibration table), a list, and per game the execution strip, a timeline
chart and the full table. What's missing is **the view across games**. Proposed layout: a summary band above the list,
filtered by the same queue / side / patch selects.

| Chart | Shows | Data (already stored) |
| --- | --- | --- |
| **Calibration plot** (replaces the table) | predicted vs actual win rate per bin, dots sized by games, the diagonal as reference | `/api/model-check` |
| **Win-rate trend** | rolling 10-game win rate with predicted P(win) as a second line: is the gap draft or play? | `drafts.pred_win_prob`, result |
| **Draft vs execution split** | stacked bars per game: draft / early / conversion share of each result | `game_execution` |
| **Side and queue split** | small multiples: blue vs red, Flex vs Ranked 5s, W–L and predicted | drafts |
| **Per-player champion grid** | each player's champions: games, win rate, mean lane gold lead at 15 (dot plot) | `own_game_participants`, timelines |
| **Ban effectiveness** | our most-banned champions and what P(win) moved when they were banned | drafts, bans |
| **Followed our #1?** | win rate when we took the top suggestion vs not | drafts (live games only; imported ones excluded) |
| **Plan adherence** | win rate by plan followed / partly / not | `drafts.plan_json` |

Notes:
- Use the validated colours (teal / amber for favourability, blue / red for sides) and always print the number too.
- Small samples: show the n on every point and a shaded interval (Wilson) instead of a bare percentage. With tens
  of games, a 60% that is really 3 / 5 should look as weak as it is.
- Per game, add a **"what would have changed it"** line: the step with the biggest swing already exists
  (`review.biggest_swing`); add its best alternative and the pp it would have gained.
- Charts: inline SVG Svelte components like `Timeline.svelte`, so no new dependency. A charting library would need
  your OK (the spec's stack table).

## 3. Engine: reliability, responsiveness, enemy pools and lanes, fewer outside lookups

**Responsiveness in Live**
- `LiveController.handle_session` rebuilds the whole view (engine, scouting blend, synergy v2, plan bar) on every
  LCU event, synchronously. Hovers fire many events a second. Proposed: coalesce events (keep only the newest
  session, ~100 ms), build off the event thread, and publish a cheap "board only" payload (slots, bans, timer) first,
  then the suggestions when they're ready.
- Time each build (`perf_counter` around `build_live_view`, logged when over 250 ms, and shown in the status line in
  dev) and add a performance test: a recorded Ranked 5s / Flex session replayed event by event must stay under a
  budget per event.
- Cache what doesn't change within a champ select: the per-role meta weights, the scouting blend per player
  (it only changes when a fetch lands), the role-share table.

**Reliability**
- Today one failure anywhere turns the whole screen into "Suggestion error (see server log)". Isolate the panels:
  if the comp panel or the plan bar fails, still show the board and the suggestion cards, with a small "comp panel
  unavailable" note.
- Keep the last good suggestions on screen (marked stale) when a build fails, instead of an empty screen mid-draft.
- A replay soak test over every recording in `fixtures/lcu/` with `DRAFTASSIST_STRICT=1`: every event builds, no
  suggestion breaks a guard, no exception.
- A data health check in the status line: patch of the stats in use, crawl matches for it, and whether Live has
  fallen back to OP.GG. You would know at a glance how much to trust the numbers.

**Enemy pools and likely lanes**
- Their side shows a single role per champion. Show the **role probabilities** when they're not near-certain
  (flex picks: "Sylas mid 62% / top 31%"), and say which assignment the suggestions assume.
- For unpicked enemy slots, show **"likely next"**: the 3 most likely champions for that role from the scouting
  blend (pool + meta), with their chance. That's the "enemy pool" view during the draft, not just after a pick.
- Scouted players: next to each name, their top 3 champions with games and win rate (shown now as icons with
  tooltips), plus **one-trick** and **mastery-only (no recent games)** labels, and how sure the role guess is.
- Ban cards already split "From their pools" / "Strongest of the rest". Add who it targets ("Jungle: 41% of their
  games").
- Flex has no enemy identities (lessons learned), so all of this falls back to role shares and meta there; label it
  that way so nobody reads it as scouting.

**Fewer lookups on other sites**
- A **matchup card** on hover or click of any enemy champion: lane gold lead at 15, game win rate, how it trends with
  game length, power spikes (the timing data), and the counters from the Matchup tree, all from data we already have.
- **Item and rune hints**: out of scope today. Would need new aggregation (runes and items are in the match-v5 data the
  crawl already caches, but nothing reads them yet) and a scope decision from you. This is the most common reason to open another site.
- **Lane tips** in plain words generated from the archetype and kit tags ("ranged poke vs melee: give up the first
  wave, look for level 6"): needs reviewed tags, otherwise it repeats unreviewed guesses.
- **Win conditions after lock-in**: the locked-in screen shows win % and biggest threats. Add 2–3 lines from the team
  model and the timing curves: "your spike is 20–28 min; theirs is before 15: trade early dragons, don't contest
  herald", and which enemy player to track (their highest comfort × threat).

## Suggested order

1. Live responsiveness and panel isolation (3: responsiveness, reliability). It's what you feel every game.
2. Enemy role probabilities and "likely next" (3: enemy pools).
3. History summary band: calibration plot, win-rate trend, per-player grid (2).
4. Matchup card and win conditions (3: fewer lookups).
5. Nav grouping and Live polish (1).
6. Items and runes, only if you want to widen the scope.
