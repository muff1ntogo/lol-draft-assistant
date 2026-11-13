# Handover: the rest of the UI redesign (step 7's Tags and Rules, step 8, step 9)

Written 13 Nov 2026. It replaces the first handover (12 Nov), which covered the roster work and is done. The user will
come back to the first model afterwards for checks, optimisation and polish, so **build the remaining steps well and
write down what you could not verify**: that pass starts from this file's last two sections.

## Read first, in this order

1. `CLAUDE.md`: the project rules. The ones that bite here: the League client is read-only (no UI may offer to hover,
   pick, lock or accept), no new npm or Python dependencies without asking the user, never commit databases,
   recordings or screenshots.
2. `docs/ui-redesign.md`: the design system, the three colour rules, the plan (steps 1-9) and a "Built" note under
   each finished step. Those notes are the precedents to follow.
3. This file.

Reference pages: `web/src/Live.svelte` for the broadcast-desk look; `web/src/Prep.svelte`, `History.svelte`,
`Roster.svelte`, `Crawl.svelte` and `Settings.svelte` for finished console pages; `#/kit` (`web/src/Kit.svelte`) shows
the first ten shared components in every state.

## Branch and rules of engagement

- Work on **`claude/sharp-noether-n5ljtx`** (pull it first). **Never merge or push to `main`**: every push to `main`
  publishes a release that teammates' apps offer as an update. The user decides when that happens.
- One step at a time, in order. After each: a "Built" note under it in `docs/ui-redesign.md`, a commit with a clear
  message, and a push to the branch.
- **Tests:** write tests alongside any Python you add and run those. Do not run the full suite per step; it runs once
  at the end. The last full run (13 Nov, before the roster rewrite) was 1,631 passed, 2 skipped; the roster rewrite
  then ran `tests/test_roster.py`, `tests/test_pool_proposal.py` and `tests/test_profile.py` (38 passed).
- If a doc is ambiguous or you would need to change scope, ask the user rather than deciding.

## Where things stand

| Step | State | Commit |
| --- | --- | --- |
| Foundation: tokens, fonts, left rail, Live prototype | Done | `b64b887` |
| 1 Shared console components and `#/kit` | Done | `3f3e0fe` |
| 2 Notices and the shell | Done | `c9cd8b5` |
| 3 Prep | Done | `4607ec4` |
| 4 Review: History and Team trends | Done | `94c5b4e` |
| 5 Roster (`docs/roster-profiles.md`, Stages A-E) | Done | `f30c2c5`, `ab08aa0`, `82d2b67` |
| 6 Crawl | Done | see `git log` ("step 6") |
| 7 Settings | Done | see `git log` ("step 7 (first part)") |
| 7 Tags, Rules | **To do** | |
| 8 Live's remaining parts | **To do** | |
| 9 Clean up | **To do** | |

Still in the old layout (they render in graphite and lime through the alias tokens, and work): `Tags.svelte`,
`Rules.svelte`, and inside Live `StrategyView.svelte`, `PlanBar.svelte`, `CompPanel.svelte`, `ScoutBox.svelte`,
`PowerChart.svelte`, `ComponentBar.svelte`, `ComponentLegend.svelte`, `Champ.svelte`. The idle Live screen's markup
is in `App.svelte` (`.idle`).

## The shared components (`web/src/ui/`)

Each file starts with a comment saying what it is for and how to call it.

| Component | Use it for | Worth knowing |
| --- | --- | --- |
| `PageHeader` | a console page's title, one-line description, actions | the page's one primary button goes in `actions` |
| `Toolbar` + `ToolGroup` | the row of view controls | `ToolGroup label=` puts the caps label first; `end` pushes it right |
| `Segmented` | a choice between modes | a radio group (arrow keys); never lime; `onchange` for navigation |
| `Button` | every button | `variant` primary / secondary / ghost, `size` sm (28) / md (32), `danger`, `href`, `busy`; pass `type="submit"` inside a form |
| `Table` | every table | `columns` + `rows` (`sortable`, `num`, `format`, `tone`, `limit`, a `cell` snippet, `onrowclick`, `selected`, `maxHeight`, loading / empty / error rows), or wrap your own `<thead>` / `<tbody>` for grouped rows |
| `Panel` | one self-contained raised area | a panel inside a panel becomes a ruled section by itself |
| `Stat` | a figure with its label | `tone` colours a value that is itself signed; `delta` is the signed change beside it |
| `Notice` | the app's messages | the shell owns it (`App.svelte`, `appStatus.svelte.js`); pages don't add banner stacks |
| `Field` | inputs, selects, textareas, checkboxes | other controls go in as children; `hideLabel` inside a labelled `ToolGroup`; a bound value must not be `undefined` |
| `Tag` | a word about a row | quiet text after a middle dot; with `onclick` a bordered filter with `pressed`; `warn` adds a warning mark; `bare` drops the dot |
| `Sheet` | a task in a side sheet, or with `dialog` a yes / no box | traps focus, Escape and the backdrop close it, focus returns to the opener; `step` moves focus to each step's first field |
| `Menu` | a "more" button for actions that shouldn't be one click away | `items: [{ label, onclick, danger? }]` |
| `RadioList` | one choice where each option needs a line of explanation | `options: [{ value, label, note? }]` |
| `Toast` | a short confirmation with one action (Undo) | call `toast(text, { action, seconds })` from `ui/toast.svelte.js`; `App.svelte` mounts it once |

`<main class="console">` is the content column (24px gutters, 16px under 1180px). Section headings are 16px / 650;
sub-labels are `.label-caps`. Sections are separated by a hairline and space, not by boxes.

If a page needs something the components don't do, extend the component rather than hand-rolling a one-off, and say
so in the step's "Built" note.

## Conventions that came out of steps 1-7 (follow them)

- **Lime is only "act on this now".** A selected, locked, pinned, ticked or open thing is a state: light ink
  (`--text-1` border or outline, `--surface-2` / `--surface-3` fill), never lime. A disabled primary is grey. One
  primary button per view, in the page header (a sheet's footer for a sheet).
- **Blue and red are only the sides, on structure** (a 3px inset edge on a list or column). Never on text or a figure.
  Don't use red for "bad" or "gap": write the word with a warning mark (`Tag warn`, `--warn`); `--error` is for real
  errors only.
- **Teal and orange only colour signed figures and their bars**, with the sign in the text, and only when the value
  is outside its noise (see the `tone` helpers in `PlanSynergy.svelte`, `Team.svelte`, `roster.js`). A result is a
  word in ink (Win bright, Loss quiet).
- **Progress is not an action**: bars are 4px and grey (`--text-3`, `--text-2` for the current one). Nothing loops
  except the champ-select dot in the rail.
- **Charts** read `web/src/palette.js`: `COMPONENT` for several series, `TEXT_2` (neutral ink) for one, `FONT_UI` /
  `FONT_MONO`, `LINE` for axes and grid, `MUTED` for labels. Lime is never a data colour. A new series set must be
  validated first (command in `docs/ui-redesign.md`, "Colour"; the dataviz skill has the script).
- **Figures are mono** (`class="mono"`, or `num: true` on a column); labels are small caps; captions are 12-13px in
  `--text-3`. Messages after an action are one line under the header (`role="status"`, or `alert` with a warning mark).
- **Every state is written out**: empty, loading, error with its reason.
- **A page's first request goes out at once** (`loading.js` holds the page until it answers); debounce only later
  edits. Other pages are lazy chunks (`App.svelte` `LOADERS`); Live and what it imports stay in the main bundle.
- **Narrow windows:** tables must fit at 1100px. Mark the columns that can go with a `wideonly` class and hide them
  under 1180px (see `Roster.svelte`, `History.svelte`).
- **Mistakes already made here, don't repeat them:**
  - Svelte drops the space between an inline element and the text after it. A mark before text, or "name · tag",
    needs a margin or a flex `gap`. This happened six times.
  - Never change state while rendering (`x[key] ??= ...` in the markup throws `state_unsafe_mutation`): give every key
    its starting value up front.
  - A value bound to a `Field` must exist: fill missing keys with `''` when the data arrives.
  - Files with non-ASCII characters must be edited with the Edit / Write tools, or a Python script that reads and
    writes UTF-8. A model that rewrote the previous handover through a shell on Windows turned every dash, arrow and
    warning mark into `??`. Check `git diff` for that before committing.

## What is left

### Step 7, second part: Tags and Rules (`Tags.svelte`, `Rules.svelte`)

As `docs/ui-redesign.md` says: Tags as a `Table` with its filters in a `Toolbar` and the review flags as `Tag`s you
can click; Rules as `Table`s. Both open only from Settings > Champion data now, so give each a "back to Settings"
link like `Player.svelte`'s "back to Roster". Tags is an editor (0-3 ratings per trait, archetypes, kit tags, three
review groups): keep every edit path and its save / revert behaviour, and keep the page usable with ~170 champions
(don't render every row's editor at once if it gets slow; the old page filters to "to review" by default).

### Step 8: Live's remaining parts

As the doc says. Notes:

- Keep `D` / `S`, keep everything Live imports in the main bundle, keep motion to events only (lock-in, ban, turn
  change, timer).
- `ComponentBar` and `ComponentLegend` hard-code the three series colours; `--viz-threat` / `--viz-matchup` /
  `--viz-comp` exist in `app.css` now.
- `PlanBar` is fixed to the bottom and offset by the folded rail (`App.svelte`); the toast sits at the bottom too
  (`ui/Toast.svelte`, 24px up): make sure they don't cover each other on Live.
- `ScoutBox` is used in two places: under their rail on Live (`compact`) and on the idle Live screen.
- Strategy needs data to look at: demo mode fills some of it; say what you could not see.

### Step 9: Clean up

- Remove `#/kit`: `Kit.svelte` and its `LOADERS` / `PAGES` entries and the `kit` filter in `preloadAll`
  (`App.svelte`). Decision 11.
- Remove the alias tokens from `app.css` (`--panel`, `--gold`, `--muted`, `--blue`, `--line`, ...) once
  `rg -- '--gold|--panel|--muted|--blue|--link|--line\b|--text\b|--red|--good' web/src` finds no use, and `GOLD` from
  `palette.js`.
- Replace every hard-coded colour in components (`rg -n '#[0-9a-fA-F]{3,6}' web/src --glob '*.svelte'`) with a token
  or a `palette.js` constant.
- Remove the old roster routes no page calls any more, with their tests moved or dropped: `PUT /api/players`,
  `POST /api/players/swap`, `DELETE /api/players/bench`, `PUT` / `DELETE /api/pools/{role}/{champ_id}`,
  `POST /api/pools/import`, and `GET /api/pools` if nothing reads it (check `rg "api/pools|api/players" web/src src
  tests` first; `accept` and `lock` are still used by `Player.svelte`). The CLI's `pools import` must keep working.
- Update `CLAUDE.md`: the code map (`web/src/ui/`, `appStatus.svelte.js`, `learning/profile.py`,
  `learning/pool_proposal.py`, `api/onboarding.py`, the roster routes, `scripts/seed_demo_games.py`), a paragraph for
  `docs/roster-profiles.md` and `docs/ui-redesign.md` like the other task docs have, "Pools page" wording that is now
  "Roster" or a player's profile, and the test count.
- Run the full suite once and fix what it finds.

## How to check your work

```sh
cd web && npm ci && npm run build          # after every UI change; it must print no warnings
# a scratch data folder; start once so it loads the champions, stop, seed, start again
export DRAFTASSIST_HOME=<scratch> DRAFTASSIST_DB=<scratch>/demo.sqlite
DRAFTASSIST_LCU=demo uv run uvicorn draftassist.api.main:app --port 8765      # first start: wait ~15 s, then stop it
DRAFTASSIST_LCU=off DRAFTASSIST_DEMO_LOG=1 uv run python scripts/seed_demo_games.py
DRAFTASSIST_LCU=demo uv run uvicorn draftassist.api.main:app --port 8765
```

- The seed script makes a made-up roster (`...#DEMO`), their games, rank and mastery, and 26 team games with execution
  splits. It refuses to run without `DRAFTASSIST_DB` and on a database that already has players. Restart the app after
  a Python change, and after the first `npm run build` (it mounts `web/dist` at start).
- Screenshots with Playwright (`require('playwright')`, pre-installed; don't install another) at **1440 x 900 and
  1100 x 760**, saved in a scratch folder, never in the repository. Move the mouse off the rail first
  (`page.mouse.move(900, 860)`), wait ~2 s, use `fullPage: true` for long pages.
- For each page: no console errors (listen for `pageerror`), `document.documentElement.scrollWidth` equals the
  viewport width at both sizes, keyboard focus visible on every control, empty / loading / error states, and reduced
  motion (`browser.newContext({ reducedMotion: 'reduce' })`).
- For data demo mode can't produce, answer the request yourself in the check:
  `page.route('**/api/...', (r) => r.fulfill({ json: ... }))`. Say in your notes when a state was checked against a
  made-up response.
- Before and after each page, run the anti-slop gate (`.claude/skills/no-ai-design-slop`, `audit-ai-design-slop`) on
  the rendered page at both widths. Ignore the landing-page, WebGL and 3D skills.

## Known gaps and things not verified (for the end pass)

- **The full suite** has not been run since the roster rewrite and steps 6-7.
- **The one real Riot check** of Add player (roster decision 7) was never done: the user has not given a Riot ID.
  Everything about onboarding is tested against a fake client or made-up answers.
- **Crawl** was only seen as a solo account with nothing crawled and nothing running. The shared-data, datasets,
  members, "what's published" and data-selection sections, and a running crawl, were restyled without being seen.
- **Settings:** only the solo account. Sign-out, the admin form and "Update to ..." were not exercised.
- **Seen only against made-up responses:** the Matchup tree's rows, the update notice, Add player's key path.
  **Not exercised at all:** the first-run progress notice in the shell, "Import missed games" on History, the error
  states of Prep and Review, reduced motion on any page after the kit.
- History's lane-matchup bars show "no data" with seeded games, so the recap's favourability bars were not seen
  with values.
- `tests/test_simulation.py::test_simulated_draft[blue-ranked]` failed once in a full run on 12 Nov while the UI was
  being rebuilt beside it, and has passed in every run since. Cause not found.
- Stage A's learning-job wiring (`backfill_game_stats`, `refresh_profiles` after the history fetch) is covered by unit
  tests of each function, not by a test of `run_learning` end to end.
- An older database gets `players.active_since` as NULL for existing players ("since" is then blank until their next
  swap). Not checked on a copy of a real database.
- "Comfort-weighted pool size" (main 1, comfortable 0.5, learning 0.25) and the lane-execution label and trend rules
  are this work's reading of the doc; the user has seen them but not confirmed them.
- The new sheet, menu, radio list and toast are not on `#/kit` (it is removed in step 9).

## What to leave behind

After each step: its "Built" note in `docs/ui-redesign.md` (what you built, choices the doc didn't spell out, what you
could not verify), a commit, a push. Keep this file's "Where things stand" table and "Known gaps" list current, and
if you stop mid-step say exactly where. Don't add new handover or review documents: put the notes in the step's
"Built" section and in this file.
