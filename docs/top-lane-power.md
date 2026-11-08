# Builds in order and lane power by milestone (v0.1.11)

Task doc for the research in `top-lane-power-research.md` (the user's research, 8 Nov 2026). Its five stages, adjusted
for what was already built in v0.1.10 (`strategy.md`: runes, spells and final items from `match_builds`) and for what
OP.GG's server turned out to offer.

## Checked before building (8 Nov 2026)

- **OP.GG's `lol_get_champion_analysis` takes a tier.** The research had this as unverified. `tools/list` shows
  `tier` (ALL, CHALLENGER, GRANDMASTER, MASTER_PLUS, MASTER, DIAMOND_PLUS, DIAMOND, EMERALD..., lowercase accepted),
  `position` and `game_mode` (ranked, flex, ...). Jax top Gold vs every tier returned different game counts, so the
  filter is real. Answers come in OP.GG's compact repr.
- **`lol_get_lane_matchup_guide`** (not in the research) answers one matchup in JSON: that matchup's builds, runes and
  skill order (every tier together; no tier filter), who has lane and solo-kill advantage, a recommended play style and
  a written tip.
- **Champion names**: UPPER_SNAKE_CASE of the display name works (LEE_SIN, KAISA, DR_MUNDO); the server is lenient
  (WUKONG and MONKEY_KING both work).
- **A real 16.22 timeline**: `ITEM_UNDO` carries `beforeId` = the item whose purchase was undone (`afterId` 0); an undone
  sale has `beforeId` 0. `SKILL_LEVEL_UP` slots 1–4 are Q, W, E, R, with `levelUpType` NORMAL or EVOLVE. `LEVEL_UP`
  has `level`. Participant frames carry `championStats` (armor, magicResist, healthMax, attackDamage, abilityPower,
  attackSpeed, abilityHaste, ...) and `damageStats` every minute: the raw material for Stage 5. Participant 0 events
  (minion items, skill points at 0:00) belong to nobody and are skipped.

## Stage 1: OP.GG builds and matchup guides (built)

`data/opgg_builds.py`: per locked champion, OP.GG's analysis for the player's own tier (their bracket on the Roster
page) and, once their opponent locks, the matchup guide. Fetched by one background thread, cached on disk for a day
(`cache/opgg/builds/`); Live never waits, and the screen refreshes when one arrives. The Strategy tab shows an
**OP.GG** block: runes, spells, starting items, core, boots, fourth items, skill max and order (games and win rate
each), and **vs <opponent>**: play style, lane and solo-kill advantage, the tip, the matchup's core and skill order.
Off when the app runs with `DRAFTASSIST_LCU=off` (tests).

## Stage 2: keep the build events (built)

`data/timeline_map.parse_build_events` reads, in the same pass that already reads gold leads, each player's purchases
in order (undone ones removed), skill order and the second each level was reached, into `match_build_events` (a few
hundred bytes a player). No extra API calls. Only timelines fetched from now on have rows: timelines aren't cached, so
older matches would need a refetch (`crawl --timelines-refetch`).

## Stage 3: our own ordered builds (built)

`data/builds.summarize_order`: from those rows, the first two finished items as an ordered pair (and how often that
order holds within the pair), boots, starting items, skill max and the first three points, with median seconds for
the first finished item and level 6. What: Master+ when that database has data, else the player's bracket; how well:
the player's bracket. Gates: an option needs 50 reference games and 5% of them, else it's hidden. Shown on the Strategy
tab as **In order** next to OP.GG's block.

## Stage 4: theoretical duel power by milestone (not started)

The research's simulator: Data Dragon base stats and growth, item stats, Meraki's `lolstaticdata` for ability values
(MIT; needs downloading and caching per patch: a new data source), per-milestone duel score, plugins for kits a stat
model can't see (Kayle, Ornn). Days of work. Needs decisions (end of this doc).

## Stage 5: lane power measured from timelines (not started)

Per-minute lead change by state, a solo-kill model and a milestone event study, shrunk towards Stage 4, drawn on a
milestone axis (lvl 2, 3, 6, item 1, 9, 11, item 2, 13, 16, item 3, quest, 19, 20). Needs the per-minute frames and
kills of the two top laners stored (Stage 2 keeps levels and items, not frames yet) and about a patch of crawl.

## Decisions for you

1. **Stage 4 data source.** Meraki's `lolstaticdata` (community, MIT, parsed from the wiki) for ability values, or a
   simpler model on Data Dragon alone (base stats, growth, items; abilities ignored or hand-entered for your pools)?
2. **Stage 5 storage.** Keep per-minute frames (level, gold, XP, position, health, armour, MR, AD, AP) for the two top
   laners of every crawled match from now on (~2 KB a match)? Top only, or every lane?
3. **Game mode for OP.GG.** Solo/Duo (`ranked`, biggest sample) is used now; `flex` exists. Switch for Ranked 5s?
