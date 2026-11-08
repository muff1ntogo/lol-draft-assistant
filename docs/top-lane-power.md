# Builds in order and lane power by milestone (v0.2.x)

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

## Decisions (8 Nov 2026)

1. **Ability values from Meraki's `lolstaticdata`** (`data/meraki.py`): one ~13 MB file, cached in `cache/meraki/`
   and refreshed weekly; stale data is used if Meraki can't be reached; without it at all, curves are measured only.
2. **Per-minute frames for every lane** (`match_frames`), the first 30 minutes, zlib-compressed: ~5 KB a match
   (measured on a real 16.22 timeline; 14 KB uncompressed).
3. **OP.GG stays on Solo/Duo.**

## Stage 4: theoretical duel power by milestone (built)

`engine/power.py` (pure). Axis: lvl 2, 3, 6, item 1, 9, 11, item 2, 13, 16, item 3, 18, and lvl 19, 20 for top
(the role quest); item milestones sit at the level each finished item usually comes at in that role (measured from
`match_build_events`, default 8 / 12 / 15). For each champion and milestone: stats from Meraki's base stats and
growth plus their usual first three finished items (Data Dragon stats), ability ranks from their usual skill max, then
`DP = sqrt(DPS x EHP)` against the role's average champion in the same state: DPS over a 6 s trade (autos, and each
damaging ability at most twice; listed cooldowns under 2 s count once: ammo and traps), EHP for a 60 / 40 physical /
magic mix. The curve is DP over the role's mean: 1.0 = average. A spike is a rise beating the role's mean rise at that
milestone by one SD.

How abilities are read (`data/meraki._ability_damage`): the first effect with damage to champions (later effects are
usually the empowered version); minimum and maximum lines averaged; a "Total" line preferred, but a total over a long
effect (with a per-tick line) counts 40% in a trade; minion, monster, reduction, increase, empowered lines skipped;
ratios on AD, bonus AD, AP, the target's max / missing health and own health. Anything else is dropped and the ability
marked partly modelled.

**Checked against reputations** (real Meraki data, OP.GG cores, 20 top laners, patch 16.22): Renekton spikes at 3 and 6
and stays high; Pantheon peaks at 6 then falls behind after item 1; Nasus climbs to the top of the role by 18–20; Malphite
is strong early and drops off after item 3. **Not captured:** Kayle reads flat and low (her power is her passive and
form changes, not in the ability values), and passives, shields, healing, crowd control and item passives in general.
That's what Stage 5 corrects where there are games.

## Stage 5: lane power measured from timelines (built)

`data/power.py`, a batch job (Crawl page **Lane power**, or `draftassist power`, after a crawl): for every lane pair in
`match_frames` and every minute up to 25, the gold lead gained over the next minute, filed under the milestone each
side had reached (level from the frame, items from `match_build_events`); per champion and milestone the mean against
the role's mean there (gold per minute). Combined with the theory on one scale: a least-squares slope maps theory to
gold per minute, then `combined = w x measured + (1 - w) x slope x (theory - 1)`, `w = n / (n + 400)` minute-observations.
Patches pool with a 3-patch half-life. Champions need 30 games in a role. Stored in `power_curves`.

**On Live**: the Strategy tab draws both laners' combined curves on the milestone axis (ECharts, loaded lazily; spikes
pinned), with lines like "Your spikes: lvl 6, item 2." and "Stronger: you from lvl 2 to lvl 6; they from item 1 to
lvl 18.", and says when a curve is mostly the model (little measured data yet).

## Not done (the research's further ideas)

- The solo-kill model and the milestone event study (kills are stored in `match_frames.kills_json` for it).
- Leaving out minutes when a jungler is near (positions are stored).
- Champion plugins (Kayle forms, Ornn's upgrades, stacking champions).
- Frames and build events only exist for timelines fetched from now on; older matches need `crawl --timelines-refetch`.
