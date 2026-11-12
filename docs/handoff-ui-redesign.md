# Handover: finishing the UI redesign

Written 12 Nov 2026 for whoever builds the rest. The work so far was done by one model over one session; the user will
come back to that model at the end for debugging, polish, tests and housekeeping, so **your job is to build the
remaining steps well, not to be the last line of defence**. Leave clear notes (see "What to leave behind").

## Read first, in this order

1. `CLAUDE.md`: the project rules. The ones that bite here: the League client is read-only (no UI may offer to hover,
   pick, lock or accept), no new npm or Python dependencies without asking the user, never commit databases,
   recordings or screenshots.
2. `docs/ui-redesign.md`: the design system, the three colour rules, the plan (steps 1–9) and a "Built" note under
   each finished step with the choices made. Read those notes: they are the precedents to follow.
3. `docs/roster-profiles.md`: step 5 of the plan, a feature in five stages (A–E), with its decisions, API, data and a
   "Built" section for Stage A.

Use `web/src/Live.svelte` as the reference for the look, `web/src/Prep.svelte` and `web/src/History.svelte` as the
reference for a finished console page, and `#/kit` (`web/src/Kit.svelte`) to see every shared component in every state.

## Branch and rules of engagement

- Work on **`claude/sharp-noether-n5ljtx`**. **Never merge or push to `main`**: every push to `main` publishes a release
  that teammates' apps offer as an update.
- One step (or one roster stage) at a time, in order. Commit after each with a clear message and push to the branch.
- The user asked to see each step before the next one. If you are told to run through several, still commit per step.
- **Tests:** the user's instruction is to run the full suite **once, at the end of all the steps** (it takes ~6
  minutes). Do run the tests you write or touch while building (`uv run pytest tests/test_x.py`). The last full run
  was after step 2: 1,612 passed, 2 skipped. Stage A added 12 tests in `tests/test_profile.py`.
- If a doc is ambiguous or you would need to change scope, ask the user rather than deciding. Decisions already taken
  are in the two docs' decision tables.

## Where things stand

| Step | State | Commit |
| --- | --- | --- |
| Foundation: tokens, fonts, left rail, Live prototype | Done before this work | `b64b887` |
| 1 Shared console components (`web/src/ui/`) and `#/kit` | Done | `3f3e0fe` |
| 2 Notices and the shell | Done | `c9cd8b5` |
| 3 Prep | Done | `4607ec4` |
| 4 Review: History and Team trends | Done | `94c5b4e` |
| 5 Roster, Stage A (data and profile figures) | Done | `f30c2c5` |
| 5 Roster, Stage B (pool proposal and onboarding API) | Done | pending commit |
| 5 Roster, Stages C, D, E | **To do** | |
| 6 Crawl | **To do** | |
| 7 Settings, Tags, Rules | **To do** | |
| 8 Live's remaining parts | **To do** | |
| 9 Clean up | **To do** | |

Pages still in the old layout (they already render in graphite + lime through the alias tokens): `Pools.svelte`,
`Crawl.svelte`, `Settings.svelte`, `Tags.svelte`, `Rules.svelte`, and inside Live `StrategyView.svelte`,
`PlanBar.svelte`, `CompPanel.svelte`, `ScoutBox.svelte`, `PowerChart.svelte`, `ComponentBar.svelte`,
`ComponentLegend.svelte`, `Champ.svelte`.

## The shared components (`web/src/ui/`)

Each file starts with a comment saying what it is for and how to call it. In short:

| Component | Use it for | Worth knowing |
| --- | --- | --- |
| `PageHeader` | every console page's title, one-line description, actions | the page's one primary button goes in `actions` |
| `Toolbar` + `ToolGroup` | the row of view controls | `ToolGroup label=` puts the caps label before the group; `end` pushes it right |
| `Segmented` | a choice between modes | a radio group (arrow keys); never lime; `onchange` for navigation |
| `Button` | every button | `variant` primary / secondary / ghost, `size` sm (28) / md (32), `danger`, `href`, `busy` |
| `Table` | every table | `columns` + `rows` (with `sortable`, `num`, `format`, `tone`, `limit`, a `cell` snippet, `onrowclick`, `selected`, loading / empty / error rows), or wrap your own `<thead>` / `<tbody>` for grouped rows |
| `Panel` | one self-contained raised area | a panel inside a panel turns into a ruled section by itself |
| `Stat` | a figure with its label | `tone` colours a value that is itself signed; `delta` is the signed change beside it |
| `Notice` | the app's messages | the shell owns it (`App.svelte`, `appStatus.svelte.js`); pages don't add their own banner stacks |
| `Field` | inputs, selects, textareas, checkboxes | other controls go in as children; `hideLabel` inside a labelled `ToolGroup` |
| `Tag` | a word about a row | quiet "· word" text; with `onclick` it is a bordered filter with `pressed`; `warn` adds ⚠; `bare` drops the dot |

`<main class="console">` is the content column (24px gutters, 16px under 1180px). Section headings are 16px / 650;
sub-labels are `.label-caps`. Sections are separated by a hairline and space, not by boxes.

If a page needs something the components don't do, extend the component (as `Table` got `limit` and `tone` in step 3)
rather than hand-rolling a one-off, and note it under the step in `docs/ui-redesign.md`.

## Conventions that came out of steps 1–4 (follow them)

- **Lime is only "act on this now".** A selected, locked, pinned or ticked thing is a *state*: mark it with light ink
  (`--text-1` border or outline, `--surface-3` fill), never lime. A disabled primary button is grey. One primary
  button per view, in the page header.
- **Blue and red are only the sides, on structure** (a 3px inset edge on a list or column). Never on text or a figure.
  Don't use red for "bad" or "gap": write the word with ⚠ (`Tag warn`), and use `--error` only for real errors.
- **Teal and orange only colour signed figures and their bars**, with `+` / `−` in the text. Use a neutral colour when a
  value is inside its noise (see `tone` helpers in `PlanSynergy.svelte` and `Team.svelte`). A result is a word in ink
  (Win bright, Loss quiet), not a colour.
- **Charts** read `web/src/palette.js`: `COMPONENT` for a chart with several series, `TEXT_2` (neutral ink) for a
  single series, `FONT_UI` / `FONT_MONO`, `LINE` for axes and grid, `MUTED` for labels. Lime is never a data colour.
  A new series set must be validated first (command in `docs/ui-redesign.md`, "Colour"; the dataviz skill has the script).
- **Figures are mono** (`class="mono"`, or `num: true` on a table column); labels are small caps; reasons and captions
  are 12–13px in `--text-3`.
- **Every state is written out**: empty ("No games match these filters"), loading, error with ⚠ and the reason.
- **A page's first request goes out at once** (`loading.js` holds the page until it answers); debounce only later
  edits. Other pages are lazy chunks (`App.svelte` `LOADERS`); Live and what it imports stay in the main bundle.
- **Spacing slips to avoid:** Svelte drops the space between an inline element and following text, so "⚠ text" and
  "name · tag" need a margin or a flex `gap`, not a literal space. This happened four times.
- When a file has ⚠, −, · or →, edit it with the Edit / Write tools (or a Python script reading and writing UTF-8),
  not shell heredocs with nested quoting (`CLAUDE.md`, last lesson).

## What is left, step by step

### Step 5: Roster (`docs/roster-profiles.md`), Stages B–E

Stage A gave you: the new columns (`player_games`: kills, deaths, assists, cs, vision, duration_s; `players`: rank_json,
mastery_json, profile_fetched_at), `learning/history.py` (`game_stats`, `backfill_game_stats`, `refresh_profiles`,
`mastery_rows`), `RiotClient.match_cached`, and `learning/profile.py` (`summary` for a Roster row, `profile` for the
page, `load_inputs(db, puuid)`; `Game`, `PoolRow`, `ExecutionRow` records). Read its docstrings and
`tests/test_profile.py` before building on it.

- **Stage B**: `learning/pool_proposal.py` (pure; the thresholds are in the doc's "Automatic pool") and the onboarding
  API (lookup, start, status, create) against a fake Riot client. Reuse `profile.also_plays` and `history.mastery_rows`;
  `refresh_profiles(db, riot, only=puuid)` fetches one player's rank and mastery whatever its age. The fetch must pause
  a running crawl the way scouting does (`cache/jobs/yield.json`; see `data/scout.py`).
  **Decision 7:** one real lookup + fetch is allowed as a sanity check, with a Riot ID the user gives (ask for it), on
  a scratch database, through `RiotClient`, nothing kept. This container's proxy holds credentials for
  `euw1.api.riotgames.com` and `europe.api.riotgames.com`; never print or commit a key.
- **Stage C**: bench, activate with undo, delete with confirm, `GET /api/roster`. **Decision 6:** delete removes the
  player, their pools and their *fetched* games; logged team games (`player_games.source == "logged"`), drafts and
  execution rows stay. Add `players.active_since` here (the "since" figure on a Roster row needs it; noted in the
  doc's Built section) and set it wherever a player becomes active. `roster.json` must follow every change
  (`Services.save_roster`). The API layer adds what needs the engine: `blind.pool_health`, blind safety per champion,
  the `execution_comfort` hint.
- **Before Stage D**: build the demo data the doc's "Data" section asks for (synthetic games, mastery and rank for the
  demo roster, clearly marked), or the pages can't be checked. `scripts/seed_demo_games.py` (see below) is a start for
  team games; player games with stats, rank and mastery are still missing.
- **Stage D**: the Roster page (lineup summaries, bench table, swap sheet with an undo toast, the four-step Add player
  sheet). "Add player" is the page's primary button. Bench and Delete are separate; Delete sits in a `⋯` menu and
  asks for confirmation (`Button danger`). There is no sheet / dialog / toast / menu component yet: build them in
  `web/src/ui/` (focus trap, Escape closes, focus returns to the opener; `motion.js` has `pop` and `reveal`), add
  them to `#/kit`, and check Add player with the keyboard alone.
- **Stage E**: the profile page at `#/player/<riot-id>` (URL-encode the Riot ID: it contains `#`). It needs a route in
  `App.svelte` that takes a parameter (`pageFromHash` only knows fixed names today) and a lazy chunk.

The old `PUT /api/players`, `POST /api/players/swap` and `DELETE /api/players/bench` keep working until the old page is
gone, then go, with their tests moved to the new routes.

### Step 6: Crawl, Step 7: Settings / Tags / Rules, Step 8: Live's remaining parts

As `docs/ui-redesign.md` describes them. Notes:

- **Crawl**: progress bars are 4px, `--text-3` (`--text-2` for the current patch), never lime: progress isn't an
  action. A user-role account (signed in with an invite) must still not see the admin-only jobs.
- **Settings**: the left sub-navigation is new; Tags and Rules open from "Champion data" (Prep's links to them were
  removed in step 3, so Settings is now the only way in). The `draftassist:update` window event from "Check for
  updates" must keep reaching `appStatus.svelte.js`.
- **Live**: keep `D` / `S`, keep everything Live imports in the main bundle, keep motion to events only.
  `ComponentBar` and `ComponentLegend` still hard-code the three series colours: `--viz-threat` / `--viz-matchup` /
  `--viz-comp` exist now (`app.css`).

### Step 9: Clean up

As the doc lists, plus what steps 1–4 added to it:

- Remove `#/kit` (`Kit.svelte`, its `LOADERS` / `PAGES` entries in `App.svelte`): **decision 11**.
- Remove the alias tokens and `GOLD` from `palette.js` once nothing reads them; replace every hard-coded hex in
  components.
- Update `CLAUDE.md`: the code map (`web/src/ui/`, `appStatus.svelte.js`, `learning/profile.py`, the roster routes),
  the status notes, and a line for `docs/roster-profiles.md`.
- Run the full suite and fix what it finds.

## How to check your work

```sh
cd web && npm ci && npm run build          # after every UI change; the build must print no warnings
# a scratch data folder, demo mode; restart after a Python change, and after the first build (the app mounts web/dist at start)
DRAFTASSIST_HOME=<scratch> DRAFTASSIST_DB=<scratch>/demo.sqlite DRAFTASSIST_LCU=demo \
  uv run uvicorn draftassist.api.main:app --port 8765
```

- Screenshots with Playwright (`require('playwright')`, pre-installed; don't install another) at **1440 × 900 and
  1100 × 760**, saved in a scratch folder, never in the repository. Move the mouse off the rail first
  (`page.mouse.move(900, 860)`), wait ~2 s for the page to settle, use `fullPage: true` for long pages.
- For each page check: no console errors, `document.documentElement.scrollWidth` equals the viewport width, keyboard
  focus is visible on every control, empty / loading / error states, and reduced motion
  (`browser.newContext({ reducedMotion: 'reduce' })`).
- Demo mode has **no logged games and no crawled matches**. Seed team games with
  `scripts/seed_demo_games.py` (its docstring has the command; stop the app first, and it only runs when
  `DRAFTASSIST_DB` is set). For data demo mode can't produce (the Matchup tree's rows, a pending update), answer the
  request yourself in the test: `page.route('**/api/...', (r) => r.fulfill({ json: ... }))`. Say in your notes when a
  state was checked against a made-up response.
- Before and after each page, run the anti-slop gate (`.claude/skills/no-ai-design-slop`, `audit-ai-design-slop`) on
  the rendered page at both widths. Ignore the landing-page, WebGL and 3D skills.

## Known gaps and things not verified (for the end pass)

- The full test suite has not been run since step 2. Steps 3–4 changed only `web/`; Stage A changed Python and ran
  only `tests/test_profile.py` and its neighbours.
- `tests/test_simulation.py::test_simulated_draft[blue-ranked]` failed once in a full run while the UI was being
  rebuilt beside it, and passed alone and in the next full run. Cause not found.
- Checked only against made-up responses: the Matchup tree's rows, the update notice. Not exercised at all: the
  first-run progress notice in the shell (only on `#/kit`), "Import missed games" on History (needs a Riot key),
  Prep's and Review's error states, reduced motion on Prep and Review.
- History's lane-matchup bars show "no data" with the seeded games, so the recap's favourability bars were not seen
  with values.
- Stage A's learning-job wiring (`backfill_game_stats`, `refresh_profiles` after the history fetch) is covered by unit
  tests of each function, not by a test of `run_learning` end to end.
- Stage B's fake-client proposal/onboarding coverage passed (38 focused tests with its affected pool/roster/profile
tests), but the user-authorized real EUW sanity lookup/fetch was not run: no `RIOT_API_KEY` was available in this
environment. It sent no request and the disposable scratch directory was removed.
- "Comfort-weighted pool size" (main 1, comfortable 0.5, learning 0.25) and the lane-execution label and trend rules
  are this work's reading of the doc; the user has seen them but not confirmed them.
- Roster's left gutter doesn't line up with the notice strip until `Pools.svelte` moves to `.console`.

## What to leave behind

After each step or stage: a "Built" note under it in the task doc (what was built, the choices you made that the doc
didn't spell out, what you could not verify), a commit per step, and the branch pushed. If you stop mid-step, say
exactly where in this file's table. Keep this file's "Where things stand" and "Known gaps" current: the end pass
starts from them.
