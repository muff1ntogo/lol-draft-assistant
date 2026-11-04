# Task: archetype matchup trees, kit interactions, lane vs game, 2v2 bot lane and blind-pick logic

Read `CLAUDE.md`, `docs/SPEC.md`, `docs/ban-score.md`, `docs/ban-value-matchups.md` and `docs/synergy-v2.md` first. Build it in the stages below and stop after each for my review. Plan first, show me the plan, then build. Write tests first.

## Source material

Five TheRealRaven guides (Oct–Nov 2026), transcripts read in full:
- **Top**, **ADC**, **Jungle** and **Mid** "Matchup Tree" guides: role archetypes and who beats whom.
- **A drafting guide:** blind picks, counter picks, who starts fights, frontline.

We take their **structure and directions** (archetypes, which beats which, what makes a pick blindable). Every **effect size comes from our data**. Win rates quoted in the videos (Master+ stats such as "Hecarim beats Qiyana 57%") are not used as numbers.

What's wrong in our engine today:
1. **Sparse matchups collapse to "even".** Lane matchups shrink towards 50% (`PAIR_K = 200`, `data/aggregate.py`). With about 800 crawled games per tier (1 Nov), most champion-vs-champion matchups read as roughly 0pp.
2. **Lane and game are mixed up.** The videos repeatedly separate *winning lane* from *winning the game*: tanks and scalers lose lane but not the game; lane bullies and Jayce win lane and then often lose. We judge matchups on game win rate only, and use `lane_adv` (`laningPhaseGoldExpAdvantage`, already parsed) only for the `lane_pressure` trait.
3. **Bot lane is scored 1v1** (ADC vs ADC), but "there's two people in lane, not just you and the enemy ADC".
4. **Every role's matchups count equally.** The jungle video: jungle matchups are "the least important"; bad archetype matchups often still give average or winning win rates.
5. **Counter picks ignore comfort.** The drafting video: "between an iffy matchup on your main and an amazing matchup on a champion you've never played, lock in your main." Our comfort weight is β = 0.3 against meta α = 1.5 (Settings). Counter suggestions can propose champions a player is only learning.
6. **No notion of who starts fights.** Our tags have `engage` and `pick`, but not the video's "champions that start fights vs champions that end fights" (setup vs follow-up).

---

## Stage 1: archetype taxonomy

Add `archetype` (primary) and optional `archetype2` per champion and role, in `data/champion_tags.yaml`. These are reviewed like other tags (`reviewed: false` = half weight; a dropdown on the Tags page). Keep the vocabulary in `data/archetypes.py`.

| Role | Archetypes | Source |
| --- | --- | --- |
| Top | `juggernaut`, `diver`, `skirmisher`, `tank`, `ranged` | Top video (Riot's class labels) |
| Jungle | `tempo_ganker`, `farmer`, `skirmisher`, `tank`, `assassin` | Jungle video |
| Mid | `artillery`, `control_mage`, `battle_mage`, `skirmisher`, `assassin` | Mid video (sorted longest to shortest range) |
| ADC | `scaler`, `lane_bully`, `poke`, `skirmisher`, `mage_bot` | ADC video (Jhin, Ashe and Varus are "poke / caster / utility": use `poke`, with `archetype2` if useful) |
| Support | `engage`, `enchanter`, `warden`, `mage`, `catcher` | Proposed (no video yet) |

- **Seed the examples the videos name** as `source: video` (still `reviewed: false`), for example:
  - Top: Darius, Garen, Mordekaiser, Nasus, Illaoi juggernaut; Sett juggernaut/diver; Renekton, Pantheon diver; Olaf, Aatrox bruiser → `diver`; Fiora, Camille, Jax, Irelia, Yone, Tryndamere skirmisher; Malphite, Ornn, Shen, K'Sante, Sion, Cho'Gath tank; Gnar, Jayce, Teemo, Kayle, Kennen, Rumble, Gangplank, Vayne, Varus ranged.
  - ADC: Jinx, Aphelios, Kog'Maw, Twitch, Vayne, Smolder scaler; Draven, Lucian, Kalista, Miss Fortune, Caitlyn, Samira lane_bully; Ezreal, Jhin, Varus, Ashe poke; Kai'Sa, Xayah, Tristana, Zeri skirmisher; Karthus, Vel'Koz, Ziggs, Xerath, Syndra mage_bot.
  - Jungle: Lee Sin tempo_ganker; Master Yi farmer; Hecarim skirmisher; Nunu, Skarner, Sejuani, Zac tank (Cho'Gath leans farmer, Nunu leans tempo: use `archetype2`); Kha'Zix, Qiyana assassin.
  - Mid: Xerath artillery; Syndra, Orianna control_mage; Ryze, Cassiopeia, Vladimir battle_mage; Yasuo, Yone, Irelia skirmisher.
- Then draft every other champion with a role share above 1%, **our five pools first**.
- Fallback for untagged champions: map Data Dragon `tags` to the nearest archetype per role, flagged `untagged`.

### Stage 1 tests
1. Every YAML archetype is in the vocabulary.
2. The fallback covers every champion in every role it's played.
3. Our five pools are fully tagged (the test lists gaps).

---

## Stage 2: kit-interaction traits (why archetypes aren't enough)

The top and mid videos stress that "a single champion ability can completely defy the rule". Examples:
- Garen's E works through Jax's E, while Darius's Q is easily dodged.
- Shen's W and Malphite's E shut down auto-attack champions like Irelia and Amumu.
- Fiora and Camille shred tanks with max-health and true damage.
- Gnar and Teemo kite champions that can't hard gap-close.
- Vex, Lissandra, Malzahar and Annie punish assassins.
- Vladimir out-sustains artillery poke.

These are **kit traits**. Add them as judgement tags (0–3, reviewed like the others):

| Tag | Meaning |
| --- | --- |
| `auto_reliant` | Damage mainly from auto attacks (Jax, Irelia, Amumu, most ADCs) |
| `auto_denial` | Blocks, dodges or slows auto attacks (Shen W, Malphite E, Jax E) |
| `max_hp_damage` | % max-health or true damage that shreds tanks (Fiora, Camille, Ornn passive, Vayne) |
| `hard_gap_close` | Reliable gap close through minions or kites (Irelia, Pantheon, Renekton, Nasus W as a slow) |
| `kite` | Can fight at range and move while doing it (Gnar, Teemo, Vayne, Jayce) |
| `escape` | Reliable disengage or escape (Kennen E, Gangplank cleanse, Syndra E) |
| `extend_fight` | Forces long fights: pulls, sticky damage (Darius pull, Shen E, Mordekaiser) |
| `anti_dive` | Kit built to punish divers or assassins (Vex, Lissandra, Malzahar, Annie) |
| `sustain` | Out-heals poke (Vladimir, Olaf, Aatrox) |
| `ability_range` | Long ability range and wave clear: the mage-bot and artillery factor (Ziggs, Xerath, Vel'Koz high; Cassiopeia low) |

Draft our pools first, then the top 30 per role, `reviewed: false`. These feed Stage 3's matchup model as interaction features, e.g. our `max_hp_damage` × their `tank`, our `auto_denial` × their `auto_reliant`, our `hard_gap_close` × their `kite`, our `anti_dive` × their `assassin` / `diver`.

---

## Stage 3: matchups smoothed by archetype and kit (hierarchical shrinkage)

Replace "shrink towards 50%" with "shrink towards what archetypes, kits and the two champions' strength predict". For our A vs their B in role r:

```
prior_logodds = logit(role_wr_A) − logit(role_wr_B) + E_r[arch(A), arch(B)] + Σ_k K_r,k · f_k(A, B)
matchup_wr    = (wins_AB + k_r · sigmoid(prior_logodds)) / (games_AB + k_r)
```

- **E_r**, the archetype matrix: antisymmetric, `E[a,a] = 0`; secondary archetypes count at 0.3 weight. It is fitted by ridge regression on each matchup's residual (`logit(wr_AB)` minus the strength difference), weighted by games, over crawled and OP.GG matchups.
- **K_r,k**, the kit-interaction coefficients: fitted the same way, on the Stage 2 products.
- **Written priors** live in `data/archetype_matchups.yaml`, as directions only: `+` means the row archetype is favoured.
  - Prior size **±0.5pp** with prior SD 1.5pp, so data takes over.
  - Mark each with `source: video` and `reviewed: false`.
  - Each prior has a `target` of `lane` or `game` (see Stage 4). The videos' grids:
  - **Top (game):** juggernaut > tank; ranged > juggernaut; diver > ranged; tank > diver; tank > ranged; skirmisher > tank.
  - **Top (lane):** tank < everyone ("a tank never really wins lane, but that's not their job").
  - **Mid (lane and game):** each archetype beats two, as a cycle: artillery > control_mage, battle_mage; control_mage > battle_mage, skirmisher; battle_mage > skirmisher, assassin; skirmisher > assassin, artillery; assassin > artillery, control_mage.
  - **ADC (lane):** lane_bully > scaler, skirmisher; poke > scaler, skirmisher, and slightly > lane_bully; scaler = skirmisher; mage_bot > everyone.
  - **ADC (game):** scaler ≈ lane_bully (even). The video notes Jinx is behind at 15 minutes either way yet wins as often.
  - **Jungle (game):** tempo_ganker > farmer, tank; skirmisher > farmer, assassin, tank; tempo_ganker ≥ skirmisher; assassin > farmer; tank < most in matchup terms, but see Stage 4. Jungle priors are half size (±0.25pp).
- **k_r per role, by empirical Bayes** (method of moments, how much champion matchups vary around the prediction versus binomial noise), clamped to [50, 400]. The top video's point that archetype game win rates barely differ while champion-level ones change drastically becomes measurable: top should get a small E and a small `k_r` (champion data trusted sooner). Print E, K and `k_r` per role in the coverage report.
- Callers don't change (lane ban score, counter risk, Win-lane plan bans, P(win) all read through the provider). Add `confidence = games / (games + k_r)` so the UI can say "measured" or "archetype estimate".

### Stage 3 tests
1. With E = K = 0 and equal role win rates, the result equals the old 50%-shrinkage formula.
2. A 3-game matchup lands within 0.5pp of its prior. A 5,000-game one barely moves.
3. Synthetic data recovers planted effects: an archetype effect (ranged top vs juggernaut +3pp) and a kit effect (`auto_denial` × `auto_reliant` +2pp). E is antisymmetric.
4. Empirical Bayes gives a larger `k_r` for low within-archetype variance and a smaller one for high.
5. Guardrail fuzz and existing tests still pass. Response times stay within 200 / 300 ms (precompute in aggregation).

---

## Stage 4: lane matchup vs game matchup

Two separate matchup numbers, both smoothed as in Stage 3, each with its own priors and `k`:
- **Lane matchup:** expected laning-phase gold and XP advantage (`lane_adv`), shown in gold/XP and in pp.
  - Aggregate `lane_adv_sum` and `lane_adv_n` per matchup; `data.backfill` fills them.
  - pp per unit of lane advantage is calibrated by a logistic fit of game win on `lane_adv`, **per archetype**. The ADC video: lane bullies with a 300+ gold lead at 15 minutes win far more often than their average, so a lead is worth more to bullies and snowballers than to scalers and tanks, whose value doesn't come from lane gold. Print the slopes.
- **Game matchup:** the game win-rate matchup (what we have now, smoothed).

Where each is used:

| Place | Uses |
| --- | --- |
| P(win), Win-teamfights bans, overall pick value | Game matchup |
| `Resp` in the lane ban score, Win-lane plan bans, counter risk, History lane bars | Lane matchup by default; a Settings blend (default 0.5 lane / 0.5 game) |
| Card reasons | Both when they disagree, e.g. `"Loses lane −310 gold, wins game +1.2pp (tank value)"` |

**Role weight on matchups.** Fit, per role, how much the game matchup actually predicts wins (the coefficient of each role's matchup term in a logistic model of game outcomes). Scale that role's matchup terms by it, normalised so the five roles average 1. The jungle video predicts jungle will come out lowest. Show the fitted weights in Settings, with a manual override.

### Stage 4 tests
1. The lane_adv aggregation matches a hand count on fixture matches.
2. The per-archetype slopes are positive and printed.
3. With lane weight 0 and all role weights 1, outputs equal the Stage 3 outputs.

---

## Stage 5: bot lane is 2v2

- Fit a ridge model of bot-lane advantage (both bot players' `lane_adv`, then game win) on:
  - ADC vs ADC and SUP vs SUP matchups (Stage 3)
  - **cross terms**: our ADC archetype vs their SUP archetype, and our SUP vs their ADC
  - **pair terms**: our ADC archetype × SUP archetype
- **Avoid the video's confound.** "Enchanters simply have a lower win rate than engage", so raw pairings make engage look better with every ADC. Pair terms must be fitted on **residuals after each champion's own strength** (as Stat already does). Check the existing ADC-SUP rules in `data/style_rules.yaml` for any that encode a main effect rather than an interaction, and list them for me.
- Wire it in:
  - **ADC and SUP Resp and suggestions** use our locked partner (or the expected partner from the pool, weighted by tier) and the enemy bot pair.
  - The ADC video's rule of thumb becomes a reason line when it applies: `"Your Leona: lane bully partner (Samira/Draven) +2.1pp"`, `"Your Lulu: scaler partner"`.
  - Lane ban score for ADC/SUP uses the 2v2 estimate.
  - History shows one combined bot-lane bar, with the 1v1s underneath.
- **Mage bots and off-role bot laners:** use the archetype for the role they actually play in this game.

### Stage 5 tests
1. A planted cross effect (`engage` SUP vs `scaler` ADC, +2pp lane) is recovered.
2. Changing our locked support changes the ADC suggestions' bot term only.
3. With cross and pair terms zeroed, results equal two independent 1v1s.

---

## Stage 6: blind picks, counter picks and pool health (drafting video)

**Blind-pick safety** (used for early pick slots and the Prep pick order):

```
blind_safety(champ, player) = comfort(tier) + worst_plausible_counter + behind_value + safety_kit
```

- `worst_plausible_counter`: pick-rate-weighted lower tail (the worst 3 of the role's top 20 opponents) of the **Lane** matchup, after smoothing. This is "the counter doesn't decide the entire lane".
- `behind_value`, a new measured trait: the champion's win rate in games where it was behind in lane (`lane_adv` < −300), shrunk. The videos: tanks are "useful even when behind"; tank junglers have the best win rate from behind.
- `safety_kit` = f(`ability_range`, `escape`), plus wave clear where tagged ("good range or wave clear and ideally an escape tool").

**Comfort gates counter picks** ("lock in your main"):
- A counter-pick suggestion's matchup term is multiplied by a comfort factor: main 1.0, comfortable 0.7, learning 0.2 (configurable).
- Learning-tier champions can still appear, but never above a main with an even or slightly losing matchup, unless their matchup edge is more than 3pp. Add a reason when this rule decides the order: `"Your main Ahri (even) over Syndra (learning, +2.4pp)"`.
- **Flag for me:** β (comfort) = 0.3 against α (meta) = 1.5 was set on purpose, but the videos argue comfort should dominate. Run `scripts/ban_audit.py --real` and the simulated draft with β = 0.3 and β = 1.0, show me both pick lists, and let me decide. Don't change the default yourself.

**Pick order by slot:** early slots favour high `blind_safety`; late slots (when the enemy laner is known) favour the Lane-matchup counter, gated by comfort. This refines `engine/plan.py`'s contest / counter / flex ordering. Keep that formula and add `blind_safety` to the early-priority term.

**Who starts the fight.** Add two tags:
- `setup`: starts fights with independent CC (Vi, Alistar, Leona, Syndra stun, Ashe R)
- `followup`: ends or cleans up fights (Orianna R, Yasuo R, Miss Fortune R, Katarina, Viktor W)

Team rules in `data/team_rules.yaml`:
- `has_setup` (+)
- `followup_without_setup` (−)
- `needs_followup`: the mid video says Ryze, Orianna and Azir are win conditions in pro play but lose value in solo queue without team follow-up. Add a tag `needs_team` and a rule `needs_team × (setup + dive)` (+), plus a penalty when absent.

The drafting video's three questions become a **Live check strip** on our pick turns:
`Picking: early/late · Fight starter: yes/no · Frontline: yes/no`.
A "no" is highlighted, and suggestions that fix it get a reason line. The scores themselves come from the team rules; the strip only displays them.

**Pool health card** (Pools page, per player), from the drafting video's ideal pool:
- a strong **blind pick** (highest `blind_safety`)
- a **setup** champion (where the role allows)
- at least one **counter to the main's worst matchups**

Show what's present and what's missing. It's informational: no automatic changes to pools.

### Stage 6 tests
1. Blind safety ranks a high-comfort, small-worst-counter, high-`behind_value` champion above a meta champion the player is learning.
2. The comfort gate: a learning champion with a +2pp matchup ranks below a main with a −0.5pp matchup. At +3.5pp it ranks above.
3. `followup_without_setup` lowers Team for a comp of only follow-up CC. Adding a `setup` champion raises it.
4. The pool health card flags a pool with no blind-safe champion.

---

## Stage 7: matchup tree UI

- **Prep → Matchup tree tab, per role:**
  - Rows: enemy archetypes, with their most-picked champions in our bracket.
  - Columns: our player's best answer from their pool (Lane and Game matchup, with confidence), the archetype-level expectation, and a **gap flag** when the best answer is below −1.5pp.
  - A one-line "plan" per enemy archetype, taken from the video's framing, e.g. mid: `"vs battle mage: out-range and abuse in lane"`, `"vs assassin: battle mage or anti-dive"`. Store these lines in `data/archetype_matchups.yaml` so I can edit them.
- **Pool coverage strip per player:** a chip per enemy archetype, green / amber / red. Clicking red lists ban candidates and the archetypes that answer it.
- **Live:** an archetype badge on cards. When the enemy locks a champion in a lane we haven't filled, the reason can use the archetype, e.g. `"vs juggernaut (Darius): kite tops favoured: Kayle +2.8"`. Kit exceptions override the archetype line when they're the bigger term: `"Shen W blocks Jax's autos"`.

### Stage 7 tests
- Payload tests for the Matchup tree, coverage strip and check strip.
- A Playwright screenshot of the Matchup tree tab at 1920×1080.

---

## Acceptance (overall)

- `uv run pytest` passes at each stage.
- `scripts/ban_audit.py --real`, before and after, shows:
  - the share of our pools' matchups vs the top 20 opponents per role with a non-trivial estimate (|pp| > 0.5)
  - how the top 10 bans and picks changed, and why
- The coverage report prints E, K, `k_r`, the per-archetype lane-lead slopes and the per-role matchup weights.
- Prep shows each player's Matchup tree gaps and pool health card. Bot suggestions change with our support. A learning-tier counter no longer outranks a main without a large edge.

## Out of scope
- Support archetypes beyond the proposed vocabulary, until I've reviewed them (or a support video exists).
- Item or rune advice per matchup.
- In-game advice (when to gank, how to convert a lead).
