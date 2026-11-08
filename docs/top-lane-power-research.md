# Anchor top-lane power to milestones, not minutes

The most accurate practical way to model a top laner's power from level 1 to 20 is a hybrid. First, compute a theoretical "duel power" score for each milestone state (level, ability ranks, completed items, Role Quest status) from Data Dragon and Meraki static data. Then correct it with outcomes measured in the match-v5 timelines your crawler already downloads. A spike is a jump in that score between consecutive milestones, shown against a milestone axis (lvl 2, 3, 6, item 1, 9, 11, item 2, 13, 16, item 3, quest, 19, 20) instead of a clock. Nobody publishes curves like these: public tools stop at win rate by game-length bucket, which is the same signal your `engine/comp.py` power curve already computes. That leaves a real gap the app can fill, but every per-state curve will be **design reasoning that you validate yourself**, not a reproduced published method. For builds and runes, the only sanctioned public source found is **OP.GG's official MCP server** (`lol_get_champion_analysis`). Your app already talks to that server but only calls the lane-meta tool, and its tier, region and position filters are unverified. Deriving builds yourself is cheaper than it looks. Every timeline the crawler fetches for 15-minute gold leads is thrown away after two numbers are extracted, so also keeping item, skill and level events costs **zero extra API calls**, and rune pages and final items are already in the cached match JSON. The recommended order: run OP.GG analysis as the day-one build panel, add self-derived builds and the milestone event table in the same crawler pass, then fit power curves once a patch's data accumulates. Writing rune pages or item sets into the client stays excluded by design; everything is shown inside the app.

## Public tools stop at game-length buckets, which measure stomps, not lane power

No public study, tool or paper was found that estimates per-champion laning strength by level or item state. The closest public products are scaling pages. SeeMeta defines scaling as late-game win rate (36+ minutes) minus early-game win rate (0–15 minutes) on **47,387 ranked matches in patch 26.22**. Its examples include Bel'Veth at +27.4 and Mel at −24.9, with Mel winning **73.0% of games under 15 minutes**. Many champions show "—" for insufficient data ([SeeMeta](https://seemeta.com/en/lol/scaling)). lolstats.gg shows a "Win Rate by Game Length" panel per champion but publishes no bucket methodology ([lolstats.gg](https://lolstats.gg/en/champions/poppy/performance/top)). The Mel figure shows the core flaw. A game under 15 minutes is almost always a surrender or a stomp, so the bucket measures "which side of stomps this champion ends up on", not laning power. Game length is also an outcome of the whole team composition, not of the top laner.

Your app already computes this kind of signal. `engine/comp.py` takes logit(WR in games > 32 min) − logit(WR in games < 25 min), shrinks each bucket towards the champion's own win rate (k = 50), and labels a champion Early/Late beyond ±0.15. Keep it as a coarse reputation check, but it cannot locate a spike at level 6 or item 2.

Academic work has a different target. Snapshot win-prediction models show that **10-minute gold difference alone predicts the winner with 72.5% accuracy**, versus 73.1% for the full logistic model ([CMU capstone, 2024](https://www.stat.cmu.edu/capstoneresearch/spring2024/460files/team17.pdf)). Gold-indicator recurrent networks exceed 91% for "who is ahead now" ([Jung & Kim, IEEE GEM 2022](https://pure.korea.ac.kr/en/publications/win-prediction-from-the-snowball-effect-perspectives/)). The Dota 2 MOBA-Slice work values every 60-second state of 71,355 pro matches with a shared per-hero subnetwork. Its interaction branch beat the additive per-hero branch, and its predictions were weakest early in the game ([MOBA-Slice](https://arxiv.org/pdf/1807.08360)). The lesson for the app: gold is the dominant nuisance variable to control for, and additive per-champion strength terms will miss matchup interactions.

Riot's own warning applies to every empirical number here. Win rate "is an imperfect measure of power because it's conflated with player skill", and a champion balanced for high-tier play "may be noticeably stronger at less experienced levels of play" ([Riot dev blog via S@20](https://www.surrenderat20.net/2015/10/red-post-collection-dev-blog-on-data.html)). lolstats.gg's Poppy page shows about 50% win rate in Diamond versus 43% in Bronze ([lolstats.gg](https://lolstats.gg/en/champions/poppy/performance/top)). Curves for a Gold top laner should therefore be fitted on Iron–Emerald data with a tier term, which your crawl already provides.

## A theoretical duel score gives every milestone a number, even with no data

The simulation side gives you a smooth prior curve for every champion, including states too rare to measure. Build it in three data layers.

**Data Dragon** supplies per-patch base stats, per-level growth and item stats, costs and recipes. Its spell tooltips are placeholder templates (`{{ e1 }}`), and Riot notes it "is not always updated immediately after a patch" ([Riot Developer Docs](https://developer.riotgames.com/docs/lol)).

**Meraki's `lolstaticdata`** (MIT licence) parses per-rank ability values and scalings from the LoL Wiki, with the stated aim of making ability damage calculable. The maintainers ask users to cache the data ([GitHub: lolstaticdata](https://github.com/meraki-analytics/lolstaticdata)).

**CommunityDragon `.bin.json`** holds the raw game formulas (`mSpellCalculations`, with base/bonus/total stat flags). It is the ground truth, but it is hard to parse, and the only documentation found dates from 2022 ([HextechDocs](https://hextechdocs.dev/resolving-variables-in-spell-textsa/)).

Pin all three per patch. Remember that match-v5 `gameVersion` uses the client's 16.x numbering while Riot markets patches as 26.x ([CommunityDragon 16.9 index](https://raw.communitydragon.org/16.9/cdragon/arena/en_us.json)); your crawler already keys patches as "16.21".

Only one formula is verified from a fetched source: physical damage taken = raw × 100/(100 + armor), so **2,000 HP with 100 armor equals 4,000 physical effective HP**. Armor reduction also applies before penetration ([LoLTheory](https://blog.loltheory.gg/what-is-armor-league-of-legends/)). The community stat-growth formula, `base + g·(n−1)·(0.7025 + 0.0175·(n−1))`, matches lolmath's statement that stats "do not increase linearly with champion level" ([lolmath](https://lolmath-docs.pages.dev/calc/Growth)). The constants themselves are **unverified**, as are the attack-speed-ratio mechanics and the ability-haste formula `CD × 100/(100 + AH)`. Unit-test them against Practice Tool numbers before trusting them.

Top lane needs two extra levels. Patch 26.1 made the top Role Quest the only quest that raises the level cap, **to 20**. It also grants +600 XP, 12.5% more XP for the rest of the game, and either Unleashed Teleport or a 30% max-health shield on Teleport arrival ([Patch 26.1 notes](https://www.leagueoflegends.com/en-gb/news/game-updates/patch-26-1-notes/)). Riot had to patch champion-specific level-19 behaviour, for example **"Jax gains an extra 1.5% attack speed per stack at level 19"**. That proves hidden per-level tables break at the new levels ([Patch 26.1 notes](https://www.leagueoflegends.com/en-gb/news/game-updates/patch-26-1-notes/)). Whether generic stat growth simply extends to n = 19 and 20 is **unconfirmed**. Treat quest completion as its own milestone state, and flag levels 19–20 as lower-confidence until checked in game. The same patch set base crit damage to 200% for most champions and made omnivamp apply at full value, which changes sustain terms in any duel model ([Patch 26.1 notes](https://www.leagueoflegends.com/en-gb/news/game-updates/patch-26-1-notes/)).

The scoring method below is a proposal; no community-standard "power score" exists. For each state S = (champion, level, ability ranks, completed items, quest status), compute:

- **Sustained DPS** over 5-second and 10-second trade windows: autos plus abilities on cooldown with haste, after the opponent's mitigation.
- **Burst**: one full combo.
- **EHP** against physical and magic damage, plus sustain.

Combine them as `DP = √(DPS × EHP)`, or use time-to-kill against a reference "average top laner" at the same level and gold. LeagueSim's optimiser uses time-to-kill against a target with given HP, armor and MR as its objective, which is a published precedent ([LeagueSim post](https://pol1.lr.ggtyler.dev/r/KhaZixMains/comments/1jal99a/i_developed_an_opensource_damage_optimizer_tool)).

Divide by the reference's DP at the same state. That removes the generic upward trend, leaving a **relative** curve. A spike is then a jump in relative power between consecutive milestones, flagged when it exceeds about one standard deviation of all champions' jumps at that milestone. Name spikes by milestone ("lvl 11", "item 2"), never by minute.

No maintained all-champion duel simulator exists to borrow. LeagueSim covers only Kha'Zix against a dummy, without runes, skill order or summoner spells, and its author declined to extend it because it "would take too long" ([LeagueSim post](https://pol1.lr.ggtyler.dev/r/KhaZixMains/comments/1jal99a/i_developed_an_opensource_damage_optimizer_tool)). Budget for a simplified generic model plus hand-written plugins for a few champions, starting with your own pools. Item passives also need hand modelling. Give each item flat stats from Data Dragon, a typed list of passive effects, and a "modelled?" flag that the UI surfaces when a passive counts as zero.

The milestone axis for a top laner and what each state needs:

| Milestone | Where it comes from | Notes |
|---|---|---|
| Levels 2, 3, 6, 9, 11, 13, 16, 18 | `LEVEL_UP` events, ability ranks from `SKILL_LEVEL_UP` | Ultimate rank-ups at 6/11/16 |
| Role Quest complete, levels 19–20 | Quest state is not directly in timelines (unverified); infer from level > 18 | Top only; 19–20 stat growth unverified |
| Completed items 1, 2, 3 (+ T2 boots) | Rebuilt from item events, classified with Data Dragon | Add a 30–45 s "walk back to lane" delay |
| Champion-specific states | Plugin per champion | Forms, stacks, item upgrades |

Two of your pool champions show why plugins matter. These are illustrations only; check the exact levels against Meraki or the wiki for the current patch. **Kayle**'s kit changes at levels 6, 11 and 16 (ranged attacks, then wave attacks, then the full form). These are discrete steps a stat-only model would draw as a gentle slope, so her plugin should switch attack range and on-hit behaviour at those levels. Her empirical curve should show the largest deviation around LEVEL_UP 11 and 16. **Ornn**'s Masterwork item upgrades arrive in the mid-teens (your team notes say 14+). The upgraded items carry separate item IDs and appear to be granted, not purchased (inferred, unverified), so the plugin must add the upgraded stats at that level. The build parser must also map upgraded IDs back to the base legendary. For **Jax vs Camille**, the generic model handles base stats and items. Jax's level-19 attack-speed fix is a reminder that late levels need their own check.

## Timelines your crawler already pays for can correct the simulator

Match-v5 timelines give one snapshot per player per minute: level, gold, XP, CS, position, a `championStats` object and a `damageStats` object. They also include a timestamped event stream. Riot's v5 changelog lists `LEVEL_UP`, `CHAMPION_TRANSFORM`, `TURRET_PLATE_DESTROYED`, and `CHAMPION_KILL` events with `victimDamageDealt`/`victimDamageReceived` damage breakdowns ([RiotTuxedo gist](https://gist.github.com/RiotTuxedo/758ee4d88693b768a880ece93cd78663)). The frame interval is 60,000 ms ([agent-lol timeline doc](https://www.mintlify.com/mangeloni30/agent-lol/api/timeline)). Participant frames hold **no inventory**, so items must be rebuilt from `ITEM_PURCHASED`/`SOLD`/`UNDO`/`DESTROYED` events ([riotapi-schema](https://raw.githubusercontent.com/MingweiSamuel/riotapi-schema/gh-pages/openapi-3.0.0.json)).

Level and item state are therefore exact to the millisecond, but gold is known only once a minute. Timelines also cannot show fights that don't end in a kill, who started a fight, summoner and ultimate cooldowns, or stack counts. Exact sub-field names inside `championStats` and the damage arrays are **unverified**; dump one raw 2026 timeline before coding against them.

`data/timelines.py` currently reads each roughly 800 KB timeline, keeps the gold and XP lead at 15:00, and discards the rest. The single most valuable change is to extract more in that same pass: the two top laners' per-minute frames (gold, XP, level, position, health) plus their kill, item, skill and level events. That is a few KB per match. Already-processed matches would need their timelines re-fetched, so make the change before the next large crawl.

From those rows, three estimators make a sensible ladder. All are reasoned designs with no published precedent.

**Densest: per-minute lead change by state.** For every minute, regress the change in the top laners' gold and XP difference on the state at the start of that minute: level difference, completed-item difference and champion identities. Use cluster-robust errors by match, and exclude frames where a jungler is near top (from `position`). This captures CS and plate pressure, i.e. lane dominance that never produces a kill, and gives many observations per game.

**Most interpretable: a state-conditioned 1v1 kill model.** Take top-vs-top `CHAMPION_KILL` events before about 14 minutes where `assistingParticipantIds` is empty, or where the victim's damage came only from the lane opponent. Fit P(A kills B | a solo kill happened) = σ(θ_A(s_A) − θ_B(s_B)). Here θ_c(s) is a global level and item curve plus a champion deviation, shrunk towards zero with L2 or a hierarchical prior. A spike is a cell where the deviation jumps.

**Robustness check: competing-risks survival.** Model time to the first top-vs-top kill with `lifelines`, using time-varying level and item covariates. Games where nobody dies count as censored observations.

All three share the same biases. Only lethal fights are observed, the player who is ahead starts more fights, and the state is partly caused by earlier kills. Controlling for cumulative gold difference reduces this endogeneity but does not remove it.

To anchor everything to milestones, run a difference-in-differences event study. Let τ be the moment a champion completes item 1, hits level 6 or reaches level 11. Compare the kill hazard or gold-lead slope in [τ−3 min, τ) with [τ, τ+3 min), against the average champion's jump at the same milestone. Tabulate state as relative to the opponent (levels 6 vs 5, item 1 vs 0) rather than absolute, because laning power is relative.

Stacking champions such as Sion and Nasus need proxies, because stacks are not in timelines. Sion's passive max health beyond what items and levels explain is observable in `championStats.healthMax` (unverified field name).

Combine the two halves with shrinkage. Use the simulator's predicted duel win probability as an offset or prior mean, then weight empirical cells by n/(n + k), with k tuned by holdout log-loss. The data overrides the simulator where evidence is thick and defers to it where cells are thin.

Validate by:

- splitting train and test by match ID, ideally by patch;
- reliability plots and Brier score against level-difference-only and gold-difference-only baselines;
- 1,000-resample cluster bootstraps for each champion-by-state cell, greyed out below about 30 events;
- checking reputations: Renekton and Pantheon should read early, Kayle late.

Sample size is the binding constraint. Personal keys allow **20 requests per second and 100 per 2 minutes** ([Riot Developer Portal](https://developer.riotgames.com/docs/portal)). Your repo quotes about 1,800 matches per hour. A typical game yields only roughly 0–3 solo top kills (an estimate), so tens of thousands of matches give per-champion event counts in the hundreds to low thousands but per-matchup counts in the tens. Fit per champion with matchup shrinkage, pool Iron–Silver with Gold–Emerald using a tier term, and draw item-specific cells (Trinity Force first versus Sundered Sky first) only for popular champions.

## OP.GG's MCP is the only sanctioned build feed, and its filters are unverified

No stats site offers a documented public build API, and Riot's API has no build aggregates. The single sanctioned channel found is OP.GG's official MCP server at `https://mcp-api.op.gg/mcp`. Its `lol_get_champion_analysis` tool returns "win, pick, and ban rates, recommended builds (items, runes, skills, spells), counters, and team synergies". Most tools take a required `desired_output_fields` parameter in dot notation, and runes and skill order exist only inside that analysis tool ([Glama README mirror](https://glama.ai/mcp/servers/@opgginc/opgg-mcp/tree)). The project is MIT-licensed, publishes no rate limits or terms, and has renamed tools across snapshots (`lol-champion-meta-data` and `lol-champion-analysis` in older listings) ([Glama tool page](https://glama.ai/mcp/servers/opgginc/opgg-mcp/tools/lol-champion-meta-data); [LobeHub](https://lobehub.com/ja/mcp/opgginc-opgg-mcp)). The interface is not frozen, so discover tool names at runtime with `tools/list` rather than hard-coding them.

Your repo settles part of the filter question. `src/draftassist/data/opgg.py` has a working streamable-HTTP JSON-RPC client (`McpClient`: `initialize`, `notifications/initialized`, `tools/call`). It currently calls **only `lol_list_lane_meta_champions`, with `{"lang": "en_US", "position": lane}`**. It passes **no tier, region or patch**, and its docstring records that the tool "has no tier filter", so it treats the data as an all-tier aggregate standing in for every bracket. It never calls `lol_get_champion_analysis`. The lane-meta tool accepts `position`, so the analysis tool plausibly does too, but that is **unverified**. Whether the analysis tool returns per-build sample sizes is also unknown.

The fix is small. Add `tools/list` to `McpClient`, save the schema, then call `lol_get_champion_analysis` once per pool champion per patch, cached like the lane meta. If no tier filter exists, label the panel "OP.GG all-tier aggregate". For core items and keystones the elo difference is usually small, but situational, win-rate-driven choices can differ more for a Gold top laner.

Everything else is scraping. U.GG serves undocumented JSON at `stats2.u.gg/lol/1.5/overview/{patch}/{mode}/{champion}/{version}.json`, with region, rank and role as keys inside one file. It does carry Iron–Emerald and EUW splits, but the only client found dates from December 2023 ([docs.rs uggo-ugg-api](https://docs.rs/uggo-ugg-api/0.1.1/src/uggo_ugg_api/lib.rs.html)). The `lolalytics-api` package calls itself an "Unofficial lolalytics scraper", exposes only tier lists and counters, and has a placeholder README ([PyPI](https://pypi.org/project/lolalytics-api/0.0.2/)). Riot's policy says products "should use supported services from Riot Games for data ingestion" and must not "remove game decisions" ([Riot Developer Policies](https://developer.riotgames.com/policies/general)). That favours OP.GG's own channel or Riot data over reverse-engineered site endpoints. It also fits the app's design of *showing* builds as options inside the app.

Some research notes suggest pushing rune pages and item sets into the client through LCU `lol-perks` and `lol-item-sets`. That **does not apply here**: the app's LCU connector is GET-only by rule and enforced by `tests/test_readonly_client.py`. LCU *read* endpoints for the client's own recommended rune pages exist in community documentation, but are unverified for late 2026 and carry no statistics.

## Self-derived builds cost no extra API calls because the crawler already fetches the data

An earlier note judged self-aggregation "too thin". That holds only for the team's own match history. Your crawler already samples EUW Iron–Emerald broadly and caches every match JSON on disk.

**Zero extra calls** gives you the final inventory (`item0`–`item5`, with `item6` as the trinket), the full rune page, summoner spells and `roleBoundItem` from the cached match DTO ([riotapi-schema](https://raw.githubusercontent.com/MingweiSamuel/riotapi-schema/gh-pages/openapi-3.0.0.json)). This is the same route `data.backfill` already uses for damage and traits. Purchase order, starting items and skill order exist only in timeline events, which `timelines.py` already downloads for the 15-minute leads. Persisting those events is a parsing change, not a crawling change.

The method below is modelled on older open-source tools. Challenger-Builds tallied timeline purchases into SQLite and kept items bought in at least 3% of games ([Challenger-Builds](https://github.com/Hippu/Challenger-Builds)). probuilds post-processed undo, sale and destroy events before ordering purchases ([probuilds](https://www.github.com/Kyle0654/probuilds)). Concretely:

- **Item order:** replay each top laner's shop actions as a stack, letting `ITEM_UNDO` cancel the latest matching action. The semantics of `beforeId`/`afterId`/`goldGain` are undocumented, so test them on real timelines.
- **Completed items:** classify with that patch's Data Dragon `item.json` (no `into`, about 2,200+ gold, not boots or consumables), with an override list.
- **Core build:** the first two legendaries as an unordered set, then the order share within that set. Report "flex" unless one order exceeds about 60%.
- **Rune pages:** canonicalise as keystone → primary row → secondary tree and picks (sorted) → shards. Show the most frequent option at each level, conditional on the one above.
- **Skill order:** the max order after the first 11 `SKILL_LEVEL_UP` events, plus the level 1–3 sequence.

For ranking, copy U.GG's rule: show "the most frequent build that holds a higher win rate than the baseline win rate" ([U.GG FAQ](https://u.gg/faq)). Treat win rates as shrunk estimates, never as rankings. By standard binomial arithmetic, telling two builds apart by 2 percentage points needs about **9,800 games per build**. RiotAugust's caution about trusting a "59% winrate" built on "only 120 games" still applies ([devtrackers.gg](https://devtrackers.gg/leagueoflegends/p/8fc2d1be-riot-august-u-gg-data-is-garbage)).

At roughly 20,000 matches per tier, a popular top laner gets about 1,600–2,400 games. That is enough for keystone, two-item core, boots and skill max, but not for matchup-specific builds. Suggested gates: 50 games and 5% share for a keystone or core, 100 games for a full page or three-item order, and 200 games for a matchup. Below those, fall back to the pooled bracket, then the previous patch, then OP.GG.

The 2025–26 parsing traps are concrete:

- **Automatic upgrades:** in 2026, Tier 3 boots come only from the mid quest and upgrade automatically ([dodge.gg](https://www.dodge.gg/lol/news/boots-guide-2026)). They, Ornn Masterwork upgrades and support-item stages must be mapped back to their base items.
- **Removed runes:** patch 26.9 removed Opportunity and Trailblazer and replaced Phase Rush with Stormraider's Surge ([AltChar 26.9](https://www.altchar.com/game-news/league-of-legends-patch-26.9-adds-new-items-reworks-statikk-shiv-and-revamps-runes-aer5Z7C52oB7)). Rune maps must be pinned per patch.
- **Teleport may be free:** the top quest can grant Teleport the player didn't take, so verify how `summoner1Id`/`summoner2Id` record it before publishing top-lane spell rates.
- **Season boundary:** never pool pre-26.1 data with 2026 data.

## Build the build panel first, then the event table, then the curves

| Stage | What to build | Cost | Confidence |
|---|---|---|---|
| 1 | Add `tools/list` to `McpClient`, then call `lol_get_champion_analysis` per pool champion per patch. Show items, runes, skills and spells in-app with an "all-tier OP.GG" label | Hours; ~10–20 MCP calls per patch | Source sanctioned; filters unverified |
| 2 | Extend `timelines.py` to persist top-lane events and frames; derive runes, spells and final items from cached match JSON | No new API calls; small storage | Field semantics to verify on 5–10 real timelines |
| 3 | Self-derived builds with gates and fallbacks, shown beside OP.GG with sample sizes | Pure SQL/Python | Good for cores and keystones at Gold |
| 4 | Theoretical duel-power curve per milestone from Data Dragon + Meraki, generic model plus plugins for pool champions (Kayle forms, Ornn upgrades) | Days | Verified mitigation; growth and level 19–20 unverified |
| 5 | Empirical correction: lead-change regression, then the solo-kill model and milestone event study, shrunk to the Stage 4 prior; graph relative power on a milestone axis with 80/95% bands | Needs about one patch of crawl | No published precedent; validate with holdout and reputations |

The graph itself should use ordinal milestones on the x-axis, with spacing equal rather than proportional to time. Plot relative power (1.0 = the reference top laner at that state) with uncertainty bands, mark flagged spikes, and overlay both laners in a selected matchup so the crossover points are visible. Spikes then read like "Jax overtakes Camille at item 2; Camille leads again at 16" (an illustration of the format, not a finding). Keep the existing Early/Late label from `comp.py` as a separate whole-game reputation badge rather than mixing it into the per-state curve.

## Conclusion

The two halves of the question share one answer. The timeline you already download for gold leads is also the raw material for build order, skill order and milestone-anchored power, so the expensive part of a "build system" (the crawl) is already paid for, and the remaining work is parsing and statistics. OP.GG earns its place as a quick, sanctioned first panel and a sanity reference, not as the long-term source for a Gold-to-Emerald team, because its tier filtering is unconfirmed and your own Iron–Emerald crawl isn't.

On power curves, the honest position is that no one has validated a milestone-anchored top-lane power score in public. The simulator gives smooth, explainable curves but misses passives and kits. The data captures what actually happens but is thin and biased towards whoever is already ahead. Neither is trustworthy alone, and their disagreement is the useful signal: a champion whose empirical spike at level 11 dwarfs its simulated one is telling you which plugin to write next. Levels 19–20 are the newest and least-checked part of the curve, so the top-lane-only quest spike is where self-measured data will matter most and where public references are weakest.
