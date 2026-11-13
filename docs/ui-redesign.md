# UI redesign: one system, two moods

Task doc for the full UI redesign (started 12 Nov 2026). The design direction was settled with the user in a question
round (decisions below). The shared foundation and a working **Live** prototype are built; everything else is for
Fable to build, page by page, following this doc. Read `CLAUDE.md` and `docs/SPEC.md` first: the hard rules there
(read-only client, Live in the main bundle, no new dependencies without asking) still apply.

## Decisions (user, 12 Nov 2026)

| # | Question | Answer |
| --- | --- | --- |
| 1 | Overall feel | **Analyst console** for Prep and the analysis pages; **broadcast desk** for Live |
| 2 | Live density | **One answer, then depth**: Live answers "what do we do now" at a glance; detail is a click or hover away |
| 3 | Colours | **Start fresh**: not the midnight blue + yellow identity |
| 4 | Palette direction | **Graphite + one signal**: neutral near-black greys, one signal colour; blue and red stay for the sides |
| 5 | Signal colour | **Acid lime** `#c8f135` |
| 6 | Type | **Inter + JetBrains Mono**, bundled as woff2 (no npm dependency) |
| 7 | Live centre | **Ranked shortlist**: 3–5 picks of near-equal weight, each with one reason (not one giant hero pick) |
| 8 | Live motion | **Broadcast moments only**: lock-in, ban, turn change, timer. Nothing ambient |
| 9 | Navigation | **Slim left rail**, folding to icons during champ select |
| 10 | Deliverable before handoff | The design system and the Live prototype, plus this doc |
| 11 | The `#/kit` sample page | **Removed in step 9** (not kept behind Settings > About) |
| 12 | Notice order | offline > error > **setup and first-run** > **update** > fixture note. The update still never shows as installable from champ select to the end of the game |

## The ideology

**The product is a decision under a 30-second clock, and the evidence behind it.** Every surface either helps make the
call (Live) or builds the evidence (everything else). So there are two moods on one system:

- **Live = broadcast desk.** Big, confident and readable from arm's length while the client is open beside it. A
  scorebar says whose turn it is; two team rails carry the side colours; the centre is a ranked shortlist. The
  scorebar is the only place that is loud, and only when it's our turn.
- **Everything else = analyst console.** Dense, precise and calm, like a trading terminal or Linear. Hairline rules
  instead of boxes, tabular figures in mono, small caps labels, one primary action per view. Analysis is the content;
  the chrome gets out of its way.

Shared by both: the tokens, the type, the 4px grid, the motion vocabulary, and three colour rules:

1. **Lime means "act on this", nothing else.** Your turn, the top pick's edge and rank, the one primary button on a
   view, the keyboard focus ring, the active rail item's edge. Never a data colour, never a decoration, never a wash
   over a large area (on graphite a lime wash turns olive: tried and rejected on Live). A disabled primary button is
   grey, not dim lime.
2. **Blue and red mean the two sides, and only mark structure.** Team rails' edges, the scorebar's side tags, chart
   series that are a side. Never on a number.
3. **Favourability (teal ahead `--up`, orange behind `--down`) only colours signed figures and bars,** always with
   `+` / `−` in the text. Orange-behind and side-red can't be told apart (normal-vision ΔE 6.9, dataviz validator), so
   they must never sit side by side as the only signal: a favourability figure is never placed on a side-coloured edge.

The anti-slop rule for this project (`no-ai-design-slop`): every element must state its job. No gradient text, no glow,
no glass, no decorative grid, no icon tiles, no pills that restate a heading, no cards inside cards. Proximity before
containers, hierarchy before labels.

## What is built (branch `claude/sharp-noether-n5ljtx`)

- **Tokens** in `web/src/app.css` (`:root`): surfaces, ink, signal, sides, favourability, status, type, spacing,
  radii, rail widths, motion. The old token names (`--panel`, `--gold`, `--muted`, …) are **aliases** onto the new
  ones, so every page not yet redesigned already renders in graphite + lime. Remove the aliases at the end (step 9).
- **Canvas palette** in `web/src/palette.js` (ECharts), same values, with the validation notes.
- **Fonts**: `web/src/fonts/` (Inter and JetBrains Mono variable, latin + latin-ext, OFL licences beside them),
  declared in `app.css`. Vite hashes them into `assets/` (served immutable).
- **Window chrome**: `window_chrome.py` title bar = `--rail`, background = `--bg`, border = `--hairline`
  (`tests/test_window_chrome.py` checks they match `app.css`).
- **Left rail** in `App.svelte`: Live, Prep, Review, Roster; Crawl and Settings at the foot; a sliding marker (one
  shared key) with a lime edge; a pulsing lime dot on Live while champ select is on (replaces the old "Champ select
  live →" link); folds to 56px on Live during champ select and below 900px, and opens over the page on hover or focus.
  `PlanBar` is offset by the folded rail.
- **Live** (`Live.svelte`), every mode the old page had, same data:
  - Scorebar: our side tag ("Red · Us") on the left, phase + turn in the middle (lime when it's ours), the timer in
    mono, their tag on the right, and a 3px timer bar along the bottom that drains from the longest time seen this
    step (the client never says how long a turn is, so the bar doesn't claim a length).
  - Draft / Strategy switch with `D` / `S` key hints.
  - Team rails: role in mono, champion, name, badge; a 3px side-coloured edge once a slot is locked, grey before;
    "you" on the local player's slot; hovers dimmed with "hovering"; enemy likely picks with their percentages; scouting
    details unchanged; bans under each rail.
  - Centre: headline (the step), a hairline **ranked shortlist** (rank in mono, icon, name, role, tags as quiet
    "· word" text instead of pills, one reason line, flags for contested / low data / warnings with ⚠, the component
    bar on the right for bans). Row 1 gets the lime edge and lime rank and a slightly larger icon: near-equal weight,
    not a hero card. Enemy turn: their probability where our rank would be, our answer on the right with a signed pp.
    Locked: the win probability in mono at 88px, the raw figure beside it, threats as a list. Flex ban groups keep their
    five-column cards.
  - Motion (`motion.js`): `lockIn` (wipe in from the team's edge with a brief brightening), `banStamp` (lands from
    1.25 to 1), the turn text crossfades, the timer bar moves linearly. All turn off under reduced motion.

Screenshots of the prototype were taken in demo mode at 1440 and 1100 px wide (see "How to check your work").

## What Fable builds next

Do the steps in order. Each ends with the acceptance check: build, screenshots in demo mode at 1440 × 900 and
1100 × 760, the anti-slop gate, and `uv run pytest`. Stop after each step for the user (CLAUDE.md: one phase at a
time).

### Step 1: Shared console components

Most pages hand-roll the same things with different sizes. Make these once in `web/src/ui/` (plain Svelte 5
components with scoped styles reading the tokens; no component library):

| Component | Replaces | Spec |
| --- | --- | --- |
| `PageHeader` | each page's own `h1` + subtitle + link row | title 24px/700, one-line description in `--text-3` 13px, actions on the right; 24px top padding, 16px below |
| `Toolbar` | the rows of pill toggles, selects and sliders on Prep | one row, 8px gaps, labels as `.label-caps` before each group, wraps on narrow widths |
| `Segmented` | the yellow-outlined pill toggles (Plan / Matchup tree, In-lane / Teamfight, Blue / Red, …) | a 28px-high group on `--surface-1`, active segment `--surface-3` with `--text-1`; never lime (it's a mode, not an action) |
| `Button` | ad-hoc buttons | `primary` (lime, at most one per view), `secondary` (`--surface-2` + hairline), `ghost` (text only); 28px (console) or 32px high |
| `Table` | each page's own table styles | hairline row rules, no zebra, header in `.label-caps`, figures right-aligned in `.mono`, 32px rows (28px dense), hover `--surface-1`, sticky header |
| `Panel` | the many rounded boxes | `--surface-1`, radius 6, no border, 16px padding; a panel never sits inside another panel (use a hairline rule instead) |
| `Stat` | big numbers (win probability, totals, crawl counts) | value in mono, label in `.label-caps` under it, optional signed delta in favourability colours |
| `Notice` | the stacked banners (`AppStatus`, the fixture note, "Reconnecting…") | one strip at the top of the content column, at most one visible, priority: offline > error > setup (Riot key) > fixture note; others collapse into a count you can open |
| `Field` | inputs and selects | 28px high, `--surface-2`, hairline border, label above in 12px `--text-3` |
| `Tag` | badges and chips (`.badge`, `.chip`) | quiet text (`· Engage`) by default; a bordered 11px tag only where it's a filter you can click |

Acceptance: the components exist with a one-page sample in demo mode (a hidden `#/kit` page is fine; remove it at the
end or keep it behind Settings > About).

**Built (12 Nov 2026).** `web/src/ui/`: the ten components plus `ToolGroup` (a labelled set of controls inside a
`Toolbar`). `web/src/Kit.svelte` at `#/kit` shows each in every state: its own lazy chunk, no rail item, not preloaded.
`.console` in `app.css` is the content column every analysis page's `<main>` takes (24px gutters, 16px under 1180px).
What to know when using them:

- `Table` draws cells from `columns` (`num` = right-aligned mono, `sortable`, `format`, a `cell` snippet for custom
  cells), or wraps your own `<thead>` / `<tbody>` for tables with their own structure. It has loading, empty and
  error rows. On a `Panel` its header and hover take the panel's surface (`--table-bg`, `--table-hover`).
- `Panel` inside a `Panel` turns into a section under a hairline rule by itself.
- `Segmented` is a radio group: one tab stop, arrow keys move the choice.
- `Field` draws inputs, selects, textareas and checkboxes; other controls (a slider with its value) go in as children.
  A ticked checkbox is light grey, not lime (being on is a state, not a call to act); the focused control's border is lime.
- `Button` has `danger` for what can't be undone (red text with its word) and `href` to render a link.
- `Notice` takes a list of items and shows the most urgent (`NOTICE_ORDER`, decision 12); `compact` is Live's one line.

### Step 2: Notices and the shell

Replace the banner stack above every page with `Notice` (the API-key prompt, the synthetic-data note, offline,
`AppStatus`'s update and first-run messages). Live keeps the scorebar flush to the top during champ select: notices
there go below the Draft / Strategy switch, one line. Keep the update-not-during-champ-select rule (`updates.py`).

**Built (12 Nov 2026).** `web/src/appStatus.svelte.js` (was `AppStatus.svelte`) turns the update, first-run and
Riot-key states into notice items; `App.svelte` adds "offline" and the demo-data note and shows one `Notice` at the top
of the content column (above Review's sub-navigation). While Live is in champ select the strip isn't drawn at the top:
`Live.svelte` gets the same items and shows them as one line under the switch. From champ select to the end of the game
the Update button is disabled and says so. A failed install is an error notice with the reason.

### Step 3: Prep (`Prep.svelte`, `PlanSynergy.svelte`, `PlanBansPicks.svelte`, `MatchupTree.svelte`)

The heaviest page and the clearest console. Today: Tags → / Rules → links in the header, two toggle rows, a ring
graph, a long synergy table, three columns of ban lists, a patch overview at the bottom.

- `PageHeader` "Prep" with the Plan / Matchup tree `Segmented` on the right. Tags and Rules move to Settings
  (they're already there; drop the header links).
- One `Toolbar`: Scope, View, Threshold, Use current draft, Data, Reset.
- Layout: the synergy ring and the plan side by side (ring 60%, plan 40%), the synergy `Table` under them full width.
  The ring keeps its arcs (`arcMove`); champion chips on it get the 11px mono tier letter instead of the round
  yellow badge.
- Plan: bans and picks as three `Table`s (Win lane / Win teamfights / Combined) with the value in mono, then Pick
  order / Fill / Risks. The "Pin for next game" button is the page's one primary action.
- Patch overview stays collapsed at the bottom.

**Built (12 Nov 2026).** As above, with these choices:

- "Pin for next game" moved from the bans-and-picks section into the page header (`Prep.svelte` owns the pin now);
  "Pinned for next game" is the same button, disabled.
- In the **In-lane** scope the five columns need the width, so the graph spans the page and the plan sits under it in
  two columns; the 60 / 40 split is the Teamfight ring's.
- A champion in the plan is marked with light ink (outline on the ring, "in plan" on a card), not lime: being in the
  plan is a state. Trait bars are neutral grey (they were blue, which is a side colour).
- Side and Format belong to the bans-and-picks section, so they sit in its heading rather than in the page's toolbar.
  The side colours the edge of the pick-order list; slot codes (B1, R2) stay neutral.
- Matchup tree: pool cover is written out ("skirmisher: covered", "tank: thin"); a gap is a ⚠ tag you can open. The
  red chips and red archetype names are gone (red is a side).
- `Table` gained `limit` and a per-column `tone`; `Stat` gained `tone` for a value that is itself a signed figure.
  `--viz-threat` / `--viz-matchup` / `--viz-comp` are the validated series colours (the plan's damage split reuses them).
- Not seen with real data here: the demo database has no crawled matches, so the Matchup tree's rows were checked
  against a made-up response.

### Step 4: Review: History and Team trends (`History.svelte`, `HistoryCharts.svelte`, `ExecutionStrip.svelte`, `Team.svelte`, `Timeline.svelte`)

- History: the Overview band as `Stat`s + one chart row; the game list as a `Table` (date, result, our five, their
  five, verdict, P(win) in mono). The execution strip keeps its meaning; restyle with the tokens and the favourability
  rule.
- ECharts: read `palette.js` for every colour and `FONT_UI` / `FONT_MONO` for text; axis and grid in `LINE`,
  labels in `MUTED`. Run the dataviz validator for any new series set (see "Colour").
- Team trends: `PageHeader` + `Segmented` window (10 / 20 / all) + charts.

**Built (12 Nov 2026).** With these choices:

- Review's two pages switch from a `Segmented` in each page's header (History / Team trends); the sub-navigation strip
  in `App.svelte` is gone.
- History, top to bottom: the Overview (`Stat`s for games, win rate and the Brier score, which moved here from the
  list's side box; then the charts and the player tables), the filters as a `Toolbar`, the games as a `Table` that
  scrolls inside its own height, and the chosen game's recap under it (it was beside a narrow list before).
- A result is a word in ink (Win bright, Loss quiet), never a colour: teal and orange are for signed figures only. The
  recap's "VICTORY" banner is a 24px heading now.
- Charts read `palette.js`: the validated series set (`COMPONENT`) where a chart has several series, neutral ink
  (`TEXT_2`) where it has one (win rate by side used the series blue, which read as the blue side). The draft timeline's
  line is ink, not lime.
- In the recap the side is each team column's outer edge; lane-matchup bars sit in the middle column, away from it.
- Team trends is full width: parts as `Stat`s (coloured only when the band is clear of 0), early game and verdicts
  as tables, players as tables without boxes, styles as a table with "suggested" / "our style" written out.
- Checked with 26 simulated games seeded into a scratch database (demo mode has none), so lane matchups show "no data".

### Step 5: Roster: profiles, adding players, bench and delete (`Pools.svelte` and new pages)

A feature as well as a restyle, with its own task doc: **`docs/roster-profiles.md`** (decisions, flows, API, data and
five stages A–E). In short: the lineup as five profile summaries (pool strip, games, win rate, KDA, lane execution,
pool health) with Profile / Swap / Bench / Delete; a bench table with Swap in; a four-step Add player sheet (Riot ID →
read their games → review the proposed pool, add champions → active or bench); and a profile page per player (summary
tiles, champion pool with editing, roles, lane execution, mastery). Build its stages in order; they replace this step.

**Built (13 Nov 2026):** see the "Built" section of `docs/roster-profiles.md`. It added `ui/Sheet`, `ui/Menu`,
`ui/RadioList` and `ui/Toast` (with `ui/toast.svelte.js`; `App.svelte` mounts the one toast), and routes with a
parameter (`#/player/<riot id>`: `App.svelte` hands the rest of the address to the page as `param`).

### Step 6: Crawl (`Crawl.svelte`)

Already close to a console. Make it full width like every other page (today it's a centred column), put the crawl
form in a `Panel` with a `Toolbar`, the tier table as a `Table` with the progress bars as thin 4px bars in `--text-3`
(the current patch's bar in `--text-2`; no lime: progress isn't an action), "Other jobs" as a `Table` (job, what it
does, last run, Run as secondary; only a job you're expected to run next may be primary), Storage as a `Table`.

**Built (13 Nov 2026).** As above. The running crawl shows its figures as `Stat`s and has no pulsing dot (nothing loops
outside the rail). Tier sets are rows of tags you press; a set's name picks or clears the whole set. Each job's last
run comes from the page's own history list. Only the solo account with nothing crawled and nothing running was seen
in a browser: the admin and user sections (shared data, datasets, members, what's published, data selection) and a
running crawl were restyled blind.

### Step 7: Settings, Tags, Rules (`Settings.svelte`, `Tags.svelte`, `Rules.svelte`)

- Settings: a left sub-navigation (sections: Weights, Bans, Synergy, Scouting, Data, Riot key, Account, Champion data,
  About), one section at a time, each a stack of labelled `Field`s / sliders with the current value in mono.
- Tags: a `Table` with filters in a `Toolbar`; review flags as `Tag`s you can click.
- Rules: `Table`s.

**Built so far (13 Nov 2026): Settings only.** Nine sections in a left sub-navigation (arrow keys move between them,
the open one is remembered); Weights, Bans and Scouting are one saved set with Save in the page header, Synergy has
its own Save there. "Data" shows the data folder and links to Crawl. Tags and Rules are still the old pages.

### Step 8: Live's remaining parts (`StrategyView.svelte`, `PlanBar.svelte`, `CompPanel.svelte`, `ScoutBox.svelte`, `PowerChart.svelte`)

- Strategy is console-dense inside Live: runes / spells / items as icon rows with names, lane plan and win conditions
  as short lists, the power chart on the tokens. Keep the `D` / `S` switch.
- PlanBar: one 56px bar, hairline top rule, statuses written out (it already does), no coloured pills.
- CompPanel: Type and Style as two `Stat`s, the identity bar unchanged (its colours are validated), legend in 11px.
- ScoutBox: a `details` at the foot of their rail.

### Step 9: Clean up

- Remove the alias tokens from `app.css` (`--panel`, `--gold`, `--muted`, `--blue`, …) once `rg -- '--gold|--panel|--muted|--blue|--link'`
  finds no use, and drop `GOLD` from `palette.js`.
- Remove every hard-coded colour in components (`rg -n '#[0-9a-fA-F]{3,6}' web/src --glob '*.svelte'`): each
  becomes a token or a `palette.js` constant, except the validated data colours, which get named tokens
  (`--viz-threat`, `--viz-engage`, …).
- Update `CLAUDE.md`'s "Colour identity" lesson to point at this doc (done in this commit).

## The system in numbers

### Colour (tokens in `app.css`)

| Token | Value | Use |
| --- | --- | --- |
| `--bg` | `#0b0c0e` | window background |
| `--rail` | `#08090b` | left rail, title bar |
| `--surface-1` | `#141519` | panels, rails' slots; the chart surface |
| `--surface-2` | `#1b1d22` | inputs, hover, active rail item |
| `--surface-3` | `#23262c` | pressed, active segment |
| `--hairline` / `--hairline-strong` | `#2a2d34` / `#3a3e47` | rules and borders (1px) |
| `--text-1` / `-2` / `-3` | `#ececee` / `#b4b8c1` / `#858a95` | primary / secondary / labels and captions |
| `--signal` | `#c8f135` | act on this (rule 1); ink on it `--signal-ink` `#0b0c0e` |
| `--side-blue` / `--side-red` | `#4a8ee0` / `#e0564c` | sides, structure only (rule 2) |
| `--up` / `--down` (+ `-text`) | `#1baf7a` / `#e0702e` | favourability, signed figures only (rule 3) |
| `--warn` / `--error` | `#f0b429` / `#ff6b6b` | status, always with ⚠ or a word |

Validated with the dataviz skill's `validate_palette.js`, dark mode, surface `#141519` (12 Nov 2026): sides pass
(CVD ΔE 22.0), favourability passes (9.3), the three ban components pass (9.4), the five identities pass (13.0
adjacent). Lime is outside the series lightness band by design: it is a signal, not a series. To check a new set:

```sh
node <dataviz skill>/scripts/validate_palette.js "#hex,#hex" --mode dark --surface "#141519"
```

### Type

Inter for everything, JetBrains Mono for figures that are compared (percentages, pp, counts, timers, role codes on
Live). `font-feature-settings: "cv11", "ss01"` is set on `:root`. `.num` / `td` get tabular figures; `.mono` switches
face.

| Role | Console | Live |
| --- | --- | --- |
| Label (`.label-caps`) | 11 / 600 / +0.06em / caps | 11–13 |
| Body | 14 / 400 | 15–16 |
| Secondary / caption | 13 / 12 | 13 / 12 |
| Section heading | 16 / 650 | — |
| Page title | 24 / 700 | 34 / 750 headline |
| Champion name | 14 / 600 in tables | 20–24 / 700 in the shortlist, 17 / 650 on rails |
| Big figure | 32 mono | 40 timer, 88 win probability |

Letter-spacing tightens as size grows (−0.01em at 17px, −0.025em at 34px, −0.05em at 88px). No gradient text, no
display font.

### Space and shape

4px grid (`--s-1` 4 … `--s-7` 48). Gutters 24px (16px under 1180px). Radius 4 for controls and tags, 6 for panels,
0 inside lists (rows are separated by hairlines, not gaps). Rail 200px, folded 56px. Live's desk: 272px rails and a
fluid centre (220px rails under 1180px; one column under 820px).

### Motion

Unchanged vocabulary (`motion.js`, `CLAUDE.md` lessons): entrances ease out, movement eases in-out, exits 20% shorter,
30ms stagger, page fades one at a time. New for Live: `lockIn`, `banStamp`. Rules: the console pages animate only
what changed (a row arriving, a value updating); Live animates only events; nothing loops except the champ-select dot
in the rail; every animation has a static end state and turns off under `prefers-reduced-motion`.

## Audit of the old UI (12 Nov 2026, the evidence for these decisions)

Screenshots in demo mode at 1440 × 900 of every page. The main findings (`audit-ai-design-slop` format, removal first):

| Priority | Pattern | Evidence | Fix (in this plan) |
| --- | --- | --- | --- |
| P1 | No shared layout | Crawl is a centred column; Roster, Prep and History run full width with different gutters | One content column with 24px gutters everywhere (steps 3–7) |
| P1 | The highlight colour had two opposite meanings | Yellow for main actions and for disabled buttons (dim olive "Swap in", "Import", "Add") | Rule 1; disabled is grey |
| P1 | Live repeated its headline | "THEIR PICK" in the header and again at 44px in the centre | Scorebar says whose turn; the centre names the step |
| P2 | Banner stack | The Riot-key prompt and the synthetic-data note on every page, above the nav's content | `Notice`, one at a time (step 2) |
| P2 | Every page invents its controls | Pill toggles (Prep), outlined buttons, text links with →, five sizes of "Run" | `Segmented`, `Button`, `Toolbar` (step 1) |
| P2 | Boxes around everything | Pools' five role cards, Crawl's job tiles, Live's comp panels, Prep's nested panels | Hairline lists and tables; panels never nest |
| P3 | Tags as pills restate data | "Poke", "Protect", "Split · Front-line" pills on every Live card | Quiet "· word" text (done on Live) |

## How to check your work

```sh
cd web && npm ci && npm run build        # after every UI change
# a scratch data folder, demo mode, then open http://localhost:8765
DRAFTASSIST_HOME=<scratch> DRAFTASSIST_DB=<scratch>/demo.sqlite DRAFTASSIST_LCU=demo \
  uv run uvicorn draftassist.api.main:app --port 8765
```

Screenshots with Playwright (pre-installed; `require('playwright')`): move the mouse off the rail first
(`page.mouse.move(900, 860)`), or the rail opens on hover. Demo mode cycles through bans, picks, the enemy's turn and
the locked screen; poll `/api/live`'s `center.mode` to catch each. Save screenshots in a scratch folder, never in the
repository: `tests/screenshots/` is gitignored because it can hold real names.

Before calling a step done:

1. The anti-slop gate (`.claude/skills/no-ai-design-slop/SKILL.md`, "Compact Quality Gates" and "Removal Test") on
   the rendered page, at both widths.
2. Every state on the page: loading, empty, error, disabled, selected, hover, focus (keyboard), reduced motion.
3. Contrast: text tokens on their surface (all pass 4.5:1 except `--text-3` on `--surface-3`, 4.38:1: don't put
   captions on pressed fills). Ink on lime is 15:1.
4. `uv run pytest` (Python tests guard the API the pages read, and the window colours).
5. The user sees the screenshots before the next step.

Skills worth loading for this work (all in `.claude/skills/`): `no-ai-design-slop` (passive, always),
`audit-ai-design-slop` (before / after each page), `design-first-ui-prompting` (to write a page's spec before
building it), `animation-systems` and `optimize-web-animations` (motion), `dataviz` (built-in; every chart),
`workflow-progress-screenshots` and `iterate-until-verified` (the loop). Ignore the landing-page, WebGL and 3D skills:
this is a desktop tool, not a marketing site.

## Constraints that don't change

- Live and what it imports stay in the main bundle; other pages stay lazy chunks (`App.svelte` `LOADERS`).
- A page's first request goes out at once (`loading.js`); debounce only later edits.
- `index.html` stays `no-cache`; hashed assets stay `immutable`.
- No new npm or Python dependencies without asking (fonts are files, not packages).
- The League client stays read-only: no UI may offer to hover, pick, lock or accept.
- Colour is never the only signal: statuses are written out, figures are signed, series have legends.

## Open questions for the user

- Should the console pages get a light theme later? The system is dark-only for now; the tokens are named by role, so a
  light set can be added under `[data-theme="light"]` without touching components.
- The rail's brand mark is a lime notched square with "Draft". Keep it, or name the app properly?
