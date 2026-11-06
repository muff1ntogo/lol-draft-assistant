# Task: Draft vs execution: why did we win or lose?

Read `CLAUDE.md`, `docs/SPEC.md`, `docs/history-broadcast-ui.md` and `docs/matchup-archetypes.md` (Stages 3–5 and decision 21) first. Build it in the stages below and stop after each for review. Plan first, then build. Write tests first.

## Goal

For every logged game, split the result into three parts:
- what the **draft** gave us
- what the **early game** (to 15 minutes) changed, and which roles moved it
- what happened in **conversion** (after 15 minutes)

Every stat is compared with **what was expected for that draft**, not with zero. A scaler or a tank behind at 15 can be on plan; a lane bully behind at 15 isn't. Single games are noisy, so each part gets a noise band, and anything within it is labelled "as expected" or "inconclusive". Conclusions come from trends over many games (Stage 5).

Wording rule: neutral language everywhere ("ahead of / behind expectation"), never "fault" or "blame".

## Method

Three win probabilities:
- **P0**: the draft model's P(win) at lock-in (see decision 2).
- **P15**: a new **state model**'s P(win) given the draft *and* the game state at 15:00.
- **Result**: 1 or 0.

Then **Draft** = P0 − 50%, **Early game** = P15 − P0, **Conversion** = Result − P15. These sum exactly to Result − 50%. The early-game part splits across roles because team gold difference is exactly the sum of the five same-position gold differences. Objectives at 15 are reported as their own line ("Objectives: jungle + team").

---

## Stage 1: more from each timeline (crawl and our own games)

`data/timelines.py` already reads the 15:00 frame for `lane_gold_15` / `lane_xp_15`. Timelines aren't cached (about 800 KB each), so **extract everything below in the same pass**, for every timeline fetched from now on. All timeline field names and event types live in one module, `data/timeline_map.py`, like `traits_map.py`. Unknown or missing values become NULL, never 0.

**Per participant**, new columns on `match_participants`:

| Column | Definition |
| --- | --- |
| `lane_cs_14` | (minions + jungle minions) lead over the same-position enemy at the 14:00 frame |
| `cs_15` | own CS at 15:00 |
| `jungle_cs_10`, `jungle_cs_15` | own jungle CS at 10:00 / 15:00 (junglers mainly) |
| `level_15` | champion level at 15:00 |
| `deaths_solo_14` | deaths before 14:00 where the killer had no assists |
| `deaths_ganked_14` | deaths before 14:00 involving 2+ enemies, at least one not in the victim's lane position |
| `deaths_other_14` | any other deaths before 14:00 |
| `kp_15` | kill participation before 15:00 (kills + assists over team kills) |
| `plates_14` | turret plates destroyed by this participant before 14:00 (`TURRET_PLATE_DESTROYED` killer) |
| `wards_placed_15`, `wards_killed_15` | from ward events before 15:00 |

**Per team**, a new table `match_team_state` (`match_id`, `team_id`, then the fields):
- at 15:00: `gold_15`, `kills_15`, `towers_15`, `plates_15`, `dragons_15`, `grubs_15` (`HORDE`), `herald_15`
- at 25:00: `gold_25` (NULL if the game ended first)
- after 15:00: `dragons_after_15`, `barons`, `towers_after_15`, and `deaths_while_ahead` (team deaths after 15:00 while that team's gold lead was ≥ 2,000 at the previous minute frame)

**Our own games:** `data/postgame.py` also fetches the match's **timeline** when verifying a logged draft (one extra call per game) and stores the same derived numbers, linked to the draft by `match_id`. Only derived numbers, never the raw timeline: it contains PUUIDs and names. A backfill command covers already-logged games.

**Baseline coverage for older crawled matches:** `data.crawl --timelines-refetch --share 0.25` refetches a random sample per bracket (configurable) and prints the estimated hours at the current limits before starting. New crawl timelines get everything automatically.

### Stage 1 tests
1. A synthetic minimal timeline fixture, plus one sanitised real timeline (no PUUIDs or names survive), gives hand-checked values for every column.
2. Death classification: solo, ganked and other cases each land in the right column.
3. Σ over the five positions of the gold differences = the team gold difference at 15:00, to 1 gold.
4. Games under 15 minutes give NULLs and are marked done, as now.

---

## Stage 2: expected values (baselines from crawled games)

Every expectation uses the same shrinkage machinery as `data/matchup_fit.py` (towards strength + archetype + kit), and each comes with a **residual SD** from crawled games so Stage 3 can label noise.

| Expectation | Conditioned on | Notes |
| --- | --- | --- |
| Lane gold, XP and CS lead at 15 / 14 | our champion, their champion, role | gold already exists as the Lane matchup (mean gold lead at 15); add XP and CS the same way |
| Bot lane 2v2 gold / XP lead | both bot pairs | reuse `engine/bot_lane.py`'s model with the new targets |
| Jungle CS at 15, KP before 15 | jungle champion | champion-level shrunk means |
| Objectives by 15 (dragons, grubs, herald) | both junglers + both teams' early/late timing (`engine/comp.py`) | Poisson or logistic per objective |
| Deaths before 14 (solo, ganked) | matchup (solo), enemy jungler (ganked) | Poisson means |
| Vision (wards placed + killed per minute) | support champion | shrunk means |
| Team gold at 25 given the state at 15 | gold diff 15, comp timing for both teams | linear model; for conversion detail |

**State model, P15:** logistic regression on crawled matches with timelines, on these features (each measured against expectation, decision 1): team gold diff at 15 (per 1,000 gold); towers, plates, dragons, grubs and herald differences at 15; and **draft timing × gold diff**, because a lead is worth more to an early comp than a late one. The **draft log-odds** is the engine's P(win) for that crawled match's ten champions with roles (batch job, cached per match), computed the same way for crawled games and our own.

Check calibration: a reliability table in 10-point bins, and a Brier score against a draft-only model on held-out matches. **P15 must beat draft-only; if it doesn't, stop and show the user.**

### Stage 2 tests
1. Synthetic data with planted effects (a gold coefficient, a timing interaction) is recovered.
2. With all 15-minute features at 0, P15 equals P0.
3. A residual SD exists for every expectation, and it shrinks properly for sparse cells.
4. The calibration table and Brier comparison print in the coverage report.

---

## Stage 3: attribution per game

For each of our logged games with a timeline (`learning/execution.py`, pure functions plus a job):

1. **Draft** = P0 − 0.5; **Early** = P15 − P0; **Conversion** = Result − P15. Test that they sum exactly.
2. **Early split by role:** in the state model's linear predictor, the gold term is `b_gold × Σ_role gold_diff_role`. For each role:
   - **Lane residual** = actual gold diff − expected gold diff (Stage 2; bot uses the 2v2 expectation, split equally between ADC and support for the gold term, but shown as one bot chip first).
   - **Role share of Early (pp)** = the change in P15 from replacing that role's actual gold diff with its expected one, with the others held at actual. Report the remainder (interaction plus objectives) as **"Objectives and team"**, so the shares sum to Early exactly.
3. **Noise labels.** For each stat, z = (actual − expected) / residual SD. Label |z| < 1 "as expected", 1–2 "ahead" / "behind", > 2 "well ahead" / "well behind".
4. **Role chips** carry the stats that mean something for that role (actual · expected · label):
   - **Top / Mid:** gold, XP and CS lead; solo deaths and ganked deaths before 14 (shown separately, so a laner isn't marked down for jungle pressure); plates.
   - **Jungle:** jungle CS at 15, objectives by 15, KP before 15. Not the jungle "matchup": the jungle video calls jungle matchups the least important.
   - **Bot (ADC + Support):** combined 2v2 gold and XP lead first, then each player.
   - **Support:** vision per minute, deaths before 14.
5. **Game verdict** (one line, rules in config, evaluated in this order):
   - "Lost from ahead": P15 ≥ 60% and a loss. "Comeback": P15 ≤ 40% and a win.
   - "Draft-limited": P0 ≤ 45%, |Early| < 10pp, and a loss. "Won on the draft": P0 ≥ 55%, |Early| < 10pp, and a win.
   - "Lost in the early game": Early ≤ −10pp and P15 < 45%. "Won the early game": Early ≥ +10pp and P15 > 55%.
   - otherwise "Inconclusive".

   Each verdict lists its top two contributors, e.g. `"Lost from ahead: +14pp early (Mid well ahead), −68pp conversion (3 deaths while ahead, Baron lost)"`.
6. Store results in `game_execution` (per draft) and `game_execution_roles` (per draft and role), recomputed when the models refit.

### Stage 3 tests
1. Draft + Early + Conversion = Result − 0.5 to 1e-9; role shares + remainder = Early to 1e-9.
2. A synthetic game where every stat equals its expectation gives Early ≈ 0, every chip "as expected", and the verdict decided by P0 and the result.
3. Each verdict rule fires on a synthetic game built for it.
4. A laner's ganked deaths don't change their solo-death label.

---

## Stage 4: History, the "Draft vs execution" strip

Below the draft recap header in `History.svelte`:
- **Waterfall bar from 50%:** Draft → Early → Conversion → Result. Each segment is signed and in pp, using the validated chart palette in `CLAUDE.md` (favourability teal / amber, never red/green). Hover shows P0 and P15.
- **Verdict line** with its two contributors.
- **Five role chips** (bot shown as one combined chip, expandable to ADC and support) plus an "Objectives and team" chip. Each chip shows the label and the role's share of Early in pp, and the label is always written, not shown by colour only.
- **Expandable table:** every stat per role, with actual, expected, residual SD and z.
- **No timeline yet** (verification pending or the game was under 15 minutes): show Draft only, with "Execution data not available".
- The game list gets a small verdict chip per game.

### Stage 4 tests
- Recap payload tests for a fixture game with a timeline, one without, and one under 15 minutes.
- A Playwright screenshot of the strip at 1920×1080 and 1000 px wide.

---

## Stage 5: trends on the Team page

- **Team:** across the last 10 / 20 / all logged games (selectable), the mean Draft, Early and Conversion in pp, and counts of each verdict. Below 10 games, show "too few games for a trend" instead of numbers.
- **Per player:** mean z for lane gold lead, solo deaths before 14 and CS lead (or the role's chip stats), with a ±1 standard-error band, and per champion for champions with 5+ logged games.
- **"Biggest lever":** the single part (Draft, a role's early game, or Conversion) with the most negative mean over the window, if its band excludes 0. Otherwise "no clear pattern yet".

### Stage 5 tests
- Trend maths (means, standard errors) on fixtures. The "too few games" threshold holds. "Biggest lever" only fires when the band excludes 0.

---

## Stage 6 (optional, behind a Settings flag, default off): execution-aware comfort

With the flag on, comfort re-scoring also uses the player's mean lane-gold z on that champion (shrunk by games), so "wins but always behind in lane" and "loses but plays the lane well" are visible on the Pools page as a hint next to the tier. It does not change tiers automatically. **Ask the user before building this stage.**

---

## Acceptance (overall)

- `uv run pytest` passes at each stage.
- Every logged real Flex game with a timeline shows the strip, with a verdict, and the parts sum to the result.
- The coverage report shows the state model's calibration table and its Brier score against the draft-only model.
- The Team page shows trends, or "too few games".
- API cost is printed for the refetch sample before it runs. No raw timelines, PUUIDs or names are stored or committed.

## Out of scope
- Per-fight analysis, positions and heatmaps, item timings.
- Grading individual plays.
- Anything after 25:00 beyond the conversion counts above.

---

## Decisions

1. **The state model reads actual minus expected (user, 5 Nov 2026).** With raw gold, "P15 = P0 when all features are 0" and "Early ≈ 0 when every stat is at its expectation" can't both hold, and a draft expected to lead would get credit for that lead twice.
2. **P0 is calibrated (user, 5 Nov 2026).** Stored final P0s ran from about 10% to 75% on our first 9 games, too extreme to read as probabilities. A scale fitted on crawled games gives the P0 used for Draft; the stored figure shows on hover. Crawled games have no comfort data, so comfort first went on top, unscaled; decision 26 replaced that.
3. **Baselines pool every tier, with a bracket adjustment (user, 5 Nov 2026).** About 3,200 matches per tier is too thin per bracket, as `matchup-archetypes.md` decision 25 found. Calibration is still checked per bracket.
4. **The refetch covers everything (user, 5 Nov 2026).** About 19,500 requests, about 6.5 hours, rather than the default 25%.
5. **ADC and support are one lane for death classification (user, 5 Nov 2026).** Otherwise every 2v2 kill reads as "ganked". A bot lane death is ganked only if someone from outside the bot lane joined.
6. **Master+ games don't feed these baselines (user, 5 Nov 2026).** The team isn't Master+. The Master+ crawl still extracts the new fields, on the user's instruction.
7. **Which event fields mean what (Stage 1, 20 real games on 16.21–16.22, about 960 plates, 1,050 kills).** Every listed event and field exists. `BUILDING_KILL.teamId` and `TURRET_PLATE_DESTROYED.teamId` are the team that lost it, so both are credited to the other team. Monster types are `DRAGON`, `HORDE`, `RIFTHERALD`, `BARON_NASHOR`.
8. **Wards of type `UNDEFINED` aren't wards (Stage 1).** 39% of placed "wards" have this type, nearly all from Yorick, Shen, Jhin, Illaoi, Akshan, Ivern and Milio: ability objects. Only `YELLOW_TRINKET`, `SIGHT_WARD`, `CONTROL_WARD` and `BLUE_TRINKET` count, placed or killed.
9. **Plates aren't limited to before 14:00 any more (Stage 1).** They fell at least as late as 25 minutes, and a third have `killerId` 0 (no player credited). `plates_14` is as specified, so it undercounts what a laner did; team plates use `teamId` and count them all.
10. **Elder counts as a dragon (Stage 1).** It's a `DRAGON` monster, and it matters for conversion.
11. **Our own games have their own table (Stage 1).** `own_game_participants`, not `match_participants`, which aggregation reads: our games would otherwise feed the crawl stats. `match_team_state` is shared, since nothing aggregates it.
12. **A version marker, not `timeline_done` (Stage 1).** `timeline_done` can't tell the old two-column extraction from the new one. `timeline_version` = 2 means everything here is stored, or the game has none.
13. **Plates come off the Top and Mid chips (user, 6 Nov 2026).** `plates_14` undercounts (decision 9), and a low count could wrongly suggest a laner did little. The column stays; Stage 3's chips don't show it. Team plates stay in the state model.
14. **Expected values come from other games (Stage 2).** Every game's residuals come from models fitted on the other four folds, and one fold never trains the state model. Synergy records are the one thing fitted outside the folds, so a crawled game's own result is taken out of them exactly (`pair_stat_leak`).
15. **Draft log-odds have no home side (Stage 2).** `_terms` weighs only "our" meta by alpha, so a crawled game is scored from both views and averaged, without comfort, with both teams in the match's tier.
16. **Objectives are a ridge on blue minus red, not Poisson or logistic (Stage 2).** The state model needs differences, and a difference of counts is neither. Towers and plates get expectations too, because decision 1 needs every state feature measured against one. The doc listed dragons, grubs and herald.
17. **First real fit (6 Nov 2026, 8,378 matches of 19,500, bronze, emerald and gold so far, 1,642 held out).** State Brier 0.163, draft-only 0.250, coin flip 0.250; well calibrated in every bin. The draft scale is 0.20: raw draft log-odds are about five times too sure of themselves. The signal is real but small: the worst fifth of drafts won 45.1%, the best 53.0%. So 93.5% of calibrated P0s land in 45-55%.
18. **Herald never happens by 15:00 (Stage 2).** 0 of 16,780 team rows. The column stays at 0 and adds nothing; Stage 3's chips leave it out.
20. **The 45% and 55% verdict rules stay (user, 6 Nov 2026).** With calibrated P0 they rarely fire, which is what the data says: the draft rarely decides a game alone. Live also gets the calibrated P(win), as a small separate change after this feature.
19. **Leads were worth a little less to the earlier comp, not more (Stage 2, first fit).** `timing_gold` came out at −0.26, against the doc's premise. It's small: about −5% of a lead's value for a comp 0.2 early. Left as the data has it, with no constraint on the sign; to look at again on the full refetch. Towers came out at −0.10 with gold in the model: they mostly pay out as gold, which is already counted.
21. **A chip's label comes from what its share measures (Stage 3).** Every role's share of Early is its gold against expectation, so its label is too, or a chip could read "behind" on a positive share. That includes jungle, whose gold lead isn't one of its shown stats, as the doc asks. It's stored as the role's headline.
22. **The objectives chip speaks only for objectives that went its way (Stage 3).** It's a remainder, including the interaction between roles and towers' negative coefficient, so its share can run against its largest stat ("−9.5pp ahead" on a real game). Its label comes from the largest notable objective in the share's direction, else "as expected". Every objective stays in the table.
23. **ADC and support split the bot chip exactly (Stage 3).** Each is averaged over being taken out first and second, so the two add up to the combined chip.
24. **A verdict leads with the part it names (Stage 3).** With the two largest parts alone, a real game read "Lost in the early game: −20pp conversion, −18pp draft". The doc's order and thresholds are unchanged, and only the line's order is. As the doc has it, "Won the early game" and "Lost in the early game" don't read the result, so a game can be won early and then lost.
25. **Comfort is today's (Stage 3).** There's no record of what a player's comfort was on the day, so a past game's P0 reads comfort learned partly from that game. The stored P(win) stays available for hover.
26. **Comfort takes the draft's scale (user, 6 Nov 2026).** Unscaled, it took P0 from 19% to 88% on our 12 games while the draft alone stayed within 43-53%, and one jungler was worth about +40pp. At the draft's scale (0.21) P0 runs 40-62%, and that jungler is worth about +11pp. Twelve games can't fit this (Brier 0.233 against 0.237 unscaled), so it rests on the reasoning: comfort sits in the same over-confident formula. There's no separate scale to tune.
27. **Stage 4's choices (6 Nov 2026).** The waterfall is one row per part on a shared 0-100% axis, each starting where the last ended, so every part keeps its own written number. Verifying a game now fills in its split straight away, because its timeline arrives after the learning job has run for it. A remake gets no split and no strip.
