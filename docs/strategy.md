# Strategy tab on Live (8 Nov 2026)

Fourth task from `ui-engine-review.md` (decision 2): Live has two views, **Draft** (the suggestions, as before) and
**Strategy**, switched at the top of the Live screen (remembered per browser). Built in one stage.

## When it fills in

- A player's card appears as soon as their champion is locked: runes, spells, build and a lane plan. It updates when
  their lane opponent is locked (the build switches to the vs-opponent sample when there are enough games).
- The **game plan** (win conditions) and **Track these** (threats) appear once all ten are locked.
- Hovers don't count. Nothing is computed in the board-only pass (`docs/live-speed.md`); a failure hides only the
  Strategy panel (`panel_errors: ["strategy"]`).

## Data

- `match_builds` (`db/models.MatchBuild`): per crawled player, keystone, rune trees, the six runes, stat shards, the
  items held at the end (no trinket) and the two summoner spells. Parsed from the match-v5 JSON the crawl already
  downloads (`data/builds.parse_build`, stored by `Crawler._store`, in both the main and the Master+ database).
  Matches crawled before: `data.backfill` fills them from the Riot cache, both databases, no API calls.
- **What** to run comes from Master+ (`data/master.usable_master`, once it has 2,000 matches), else from the player's
  own bracket. **How well** it wins comes from the player's bracket (Pools page) in the main database, or every tier
  when the bracket has under 40 games. Both are shown ("71% pick · 50% win (60 g)").
- Patches pool with a half-life of one patch (builds change every patch).
- Against the lane opponent when both the reference set and the bracket have 30+ games of that matchup, else overall
  (the card says which).
- Names: Data Dragon `item.json`, `runesReforged.json` and `summoner.json`, downloaded with the champions by
  `data.ddragon` (cached per version). Without them the card shows ids and says to run `data.ddragon`.
- Item kinds: a **finished** item builds into nothing and costs 2,000+ gold; **boots** are tier-2 boots; an item
  applies **anti-heal** when its description mentions Grievous Wounds.

## What it says (`engine/strategy.py`, plain rules over measured numbers and tags)

- **Lane**: the mean gold lead at 15 vs the opponent (even under 250; strong at 600+) with what to do about it; the game
  matchup in pp and the two archetypes; then lines that only appear when the numbers or tags say so: range (attack
  range, else archetype), their hard engage vs our escape, their sustain, who wins extended trades, auto-attack denial,
  kiting, and opposite power curves.
- **Build**: top three keystones, the most common rune page and spell pair, the three most common finished items
  (core) and boots; **This game**: further items that answer their damage (magic resist when 55%+ of their damage is
  magic, armour at 62%+ physical) or 2+ high-sustain enemies (anti-heal), with the reason. Reasons below.
- **Game plan**: power curves compared (early comp: take early objectives and end before 30; later comp: trade
  objectives, avoid coin flips), what our fight identity wants, what to watch for in theirs, no-frontline warning,
  their damage split.
- **Track these**: the engine's two biggest threats, the scouted player on them, and our laner's plan against them.

## Not done

- Item order (needs timelines' purchase events) and skill order: the build is what was held at the end, by how often.
- Lane tips come from tags that are mostly unreviewed (drafted by Claude). Reviewing kit tags on the Tags page makes
  them more trustworthy.

Tests: `tests/test_strategy.py`.
