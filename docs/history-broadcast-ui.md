# Task: redesign the History tab in a tournament broadcast style

Read `CLAUDE.md` and `docs/SPEC.md` (Data model, UI) first. Plan first and show me a quick static mock (HTML with fixture data is fine) before wiring it to real data.

## Goal

Each logged game should read like a draft recap on an esports broadcast: two teams facing each other, bans across the top, picks down the sides, lane matchups in the middle showing who was favoured, and a short review of how our draft went. It must be readable at a glance on a 1080p second monitor.

## Page structure

**1. Game list (left, or on top in narrow layouts).** One compact row per game, newest first:
- result chip (WIN / LOSS)
- our 5 champion icons vs their 5
- predicted win % before the game
- queue (Ranked 5s / Flex / Clash), side, patch, date

Filters: queue, side, result, patch. Clicking a row opens the recap.

**2. Recap card (main area).**

- **Header bar:** VICTORY / DEFEAT (large), queue, patch, date, game length, our side. "Predicted 54% → Won" or "→ Lost".
- **Bans row:**
  - Blue team's bans on the left, red team's on the right, as small greyscale portraits with a diagonal strike, in the order they were banned.
  - Tournament draft: split into Phase 1 and Phase 2 with a thin divider.
  - Flex: one group of 5 per team, no phases.
  - Skipped bans are an empty slot marked "—".
- **Pick columns:** blue on the left, red on the right, one row per role (Top, Jungle, Mid, ADC, Support). Each pick shows:
  - the champion's tile (or loading-screen art, if it fits)
  - the champion name
  - our player's name (our side only)
  - the pick order tag (B1, R1, R2, B2…)
- **Centre lane matchups:** between the two columns, one row per role:
  - Matchup favourability = shrunk lane matchup win-rate delta for our champion vs theirs, in percentage points (from the `matchups` table, patch and bracket of that game).
  - Draw a **diverging bar** centred on 0, extending towards the favoured side, with the signed value as text (e.g. `+3.2pp`).
  - Colour: green when we're favoured, red when they are, grey within ±1pp. Always show the signed number too, so colour is never the only signal.
  - Show `no data` when the matchup has fewer than 50 games after shrinkage.
- **Draft review strip (bottom):**
  - **Win-probability timeline:** a small line chart of the model's P(win) after each of the 20 draft steps (x = step, labelled B1/R1/ban; y = 30–70%). Mark the biggest swing with a dot and a label.
  - **Followed suggestions?** For each of our bans and picks: the rank it had in our suggestions (1 / 2 / 3 / off-list).
  - **Biggest swing:** the single action that moved P(win) most, with its delta.
  - **Synergy:** our team's synergy total vs theirs, signed.
- Collapsible **"Full table"**: every action with step, side, type, champion, our suggestion rank and P(win) after it.

## Data

- Read from `drafts` and `draft_actions`.
- Add a nullable column `win_prob_after` to `draft_actions`. Store it at draft time from now on. For old games, compute it on first view and cache it (migration + backfill function).
- Lane matchups are computed on view from the `matchups` table for that game's patch and bracket, and cached per game.
- Champion art: Data Dragon champion square icons and loading-screen art, pinned to the game's patch version and cached on disk. Fall back to the champion name in a neutral tile if the image is missing.

## Visual style

- Dark background; blue side `#2f6fb7`-ish, red side `#c0423a`-ish. The favourability greens and reds must be distinguishable from the side colours.
- Large, bold headers; team colours as thin accent bars, not full-panel fills.
- Champion portraits: square, 64 px in picks, 40 px in bans.
- No horizontal scrolling at 1920×1080. Stack the columns below 1100 px width.
- Text contrast meets WCAG AA. Every coloured signal also has a text or number equivalent.

## API

- `GET /api/history?queue=&side=&result=&patch=` returns the list rows.
- `GET /api/history/{draft_id}` returns the recap payload: header, bans by phase, picks by role, lane matchups, timeline, review.

## Tests

1. Payload test: a tournament-draft fixture game returns bans split into 2 phases (6 + 4), picks in the correct order, and 5 lane matchups.
2. Payload test: a Flex fixture game returns 5 + 5 bans with no phases.
3. Favourability sign: our champion favoured → positive; unfavoured → negative; sparse → `no data`.
4. Backfill: an old game with no `win_prob_after` gets 20 values after first view, and they're stored.
5. Visual check: a Playwright screenshot of the recap for each fixture at 1920×1080 and 1000 px width, saved to `tests/screenshots/` for me to review. Nothing overflows the viewport.

## Acceptance

- `uv run pytest` passes.
- I can open History, click a game, and see the full broadcast-style recap for both a Ranked 5s and a Flex game.
- The screenshots look like a broadcast draft recap, not a data table.

## Out of scope

- Live screen changes.
- Any in-game stats beyond the result and game length (KDA, gold and so on), unless I ask later.
