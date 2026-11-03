# Task: Synergy v2: three-part synergy, merged Prep + Synergy page, plan-driven bans and picks

Read `CLAUDE.md`, `docs/SPEC.md`, `docs/synergy-visualizer.md`, `docs/ban-score.md`, `docs/ban-guardrails.md` and `docs/ban-value-matchups.md` first.

**This supersedes `synergy-visualizer.md`:**
- Keep its Stage A pair data collection and normalised score: that score becomes the **Stat** component below.
- Its Synergy page and engine integration are replaced by this design.
- Its damage-balance penalty moves into the **Team** component.

Build it in the 6 stages below, stopping after each stage for review. Plan first, then build. Write tests first.

---

## The model in one paragraph

Synergy has **three components**, all in log-odds and displayed in percentage points (pp = 25 × log-odds):

1. **Stat:** observed win-rate synergy of champions together (pairs, from crawled matches).
2. **Style:** how well their fighting styles mesh, from a written rule table that match data then adjusts.
3. **Team:** how well the whole five-champion comp works in teamfights outside lane.

There are two **scopes**:
- **In-lane:** the core pairs only, Top + Jungle, Jungle + Mid, ADC + Support. Top on its own has no in-lane synergy.
- **Teamfight:** all five.

Every component is shown separately, and also as an aggregate.

---

## Stage 1: champion traits (hybrid)

Each champion in each role gets a trait vector. Traits come from two sources.

### 1a. Traits measured from match stats

Computed per champion, role and synergy window (the pooled, decayed window from `synergy-visualizer.md`). Each is a **percentile within the role** (0–1).

| Trait | Source (match-v5 participant fields) |
| --- | --- |
| `cc` | `timeCCingOthers` per minute, plus `challenges.enemyChampionImmobilizations` if present |
| `frontline` | share of team damage taken, plus `damageSelfMitigated` per minute |
| `peel` | (`totalHealsOnTeammates` + `totalDamageShieldedOnTeammates`) per minute |
| `dmg_phys` / `dmg_magic` / `dmg_true` | share of damage to champions by type |
| `power_curve` | slope of win rate across game-length buckets (< 25, 25–32, > 32 min): negative = early, positive = scaling |
| `lane_pressure` | `challenges.laningPhaseGoldExpAdvantage` or `earlyLaningPhaseGoldExpAdvantage`, if present |
| `ranged` | 1 if Data Dragon `stats.attackrange` > 300, else 0 |

`challenges` keys vary by patch: the field mapping lives in one module with a test. A missing source field gives a `null` trait (not 0), ignored downstream.

### 1b. Judgement traits as tags (reviewed)

`engage`, `disengage`, `poke`, `dive`, `pick`, `splitpush`, `enchanter`, `hypercarry`, rated **0–3** per champion and role.

- Stored in `data/champion_tags.yaml` (version control) and loaded into a `champion_tags` table.
- Claude drafts the first pass (every champion in our pools, then the top 30 per role by pick rate), every entry `reviewed: false`.
- **A Tags page** shows champions with their tags; edit and mark reviewed. Unreviewed tags count at half weight in every model.
- Champions with no tags fall back to measured traits only, and are flagged `untagged` in the UI.

### Stage 1 tests
1. The trait mapping produces values for a fixture match, and a missing `challenges` key gives `null`, not an error.
2. Percentiles are within [0, 1] and computed per role.
3. `power_curve` is negative for a synthetic champion who wins short games and loses long ones.
4. Tags load from YAML. Unreviewed tags are half-weighted.

---

## Stage 2: style rules + data (pairs) and the team model

### 2a. Pair style rules (in-lane, core pairs)

`data/style_rules.yaml` holds readable rules, each with a prior effect in pp. Claude drafts about 15 rules per core pair for review:

```yaml
- pair: ADC-SUP
  when: { SUP: engage, ADC: not ranged_long }   # all-in style ADC
  prior_pp: +1.5
  note: "Engage support suits short-range all-in ADCs"
- pair: ADC-SUP
  when: { SUP: enchanter, ADC: hypercarry }
  prior_pp: +2.0
  note: "Enchanters enable hypercarries"
- pair: JG-MID
  when: { MID: lane_pressure, JG: dive }
  prior_pp: +1.2
  note: "Mid priority lets a dive jungler invade and skirmish"
- pair: TOP-JG
  when: { TOP: power_curve_late, JG: power_curve_late }
  prior_pp: -1.0
  note: "Two scalers concede the early map on that side"
```

- A rule's strength for a pair is the **product of the two trait intensities** (each 0–1). Tags scale 0–3 → 0–1; `not X` = 1 − X; derived terms (`ranged_long`, `power_curve_late`) are defined in code next to the trait mapping.
- **Fit to data:** regress each core pair's Stat residual (`s_raw`, weighted by games) on the rule-strength features, with **ridge regression centred on the priors** (prior mean `prior_pp/25`, prior SD 1pp, configurable). With little data, fitted values stay close to the written rules.
- **Style(A, B)** = Σ fitted coefficient × rule strength.
- **Explainability:** each pair's Style shows its top 3 contributing rules with their pp, e.g. `"Enchanter + hypercarry +1.7"`.
- **A Rules page** lists every rule with prior, fitted value and supporting games; flags rules whose fit differs from the prior by more than 1.5pp or flips sign; priors are editable (written back to YAML).

### 2b. Team model (teamfight scope)

`data/team_rules.yaml`: about 12 whole-comp rules with priors, covering at least reliable engage, enough frontline, peel for a hypercarry, damage balance (replaces the old 85% penalty), power-curve coherence and comp identity coherence (engage / poke / pick / protect / split). Plus **cross-team rules**, our traits vs theirs: our engage vs their disengage (−), our poke vs their hard engage (−), our pick vs their low-mobility carries (+), and similar.

- Team features from the five champions' traits (max for "has", sums for amounts, as each rule specifies).
- **Fit:** logistic regression on crawled matches, `win ~ f(our team) − f(their team) + cross(our, their)`, ridge priors from the YAML.
- **Team(comp)** = f(comp) − f(average comp in the bracket), in pp. With a (partly) known enemy comp, also **Team vs enemy**, including cross terms.
- **Comp identity label:** whichever of engage / poke / pick / protect / split the comp's tags fit best, plus a coherence %.
- Also: the damage profile bar (physical / magic / true) and trait bars (engage, frontline, peel, poke, scaling).

### Stage 2 tests
1. Synthetic data with a planted +3pp "enchanter SUP × hypercarry ADC": the fitted coefficient is positive and near 3pp, and it is predicted for an **unseen** pair with the same traits.
2. With zero data, fitted values equal the priors.
3. Synthetic team data with a planted "no frontline: −4pp": the team coefficient recovers it.
4. Cross term: our engage vs their high disengage comes out negative.
5. Editing a prior on the Rules page round-trips to YAML.

---

## Stage 3: components and aggregates

### Per core pair (in-lane scope)
- `Stat(A,B)`: the `synergy-visualizer.md` A3 score (shrunk towards 0).
- `Style(A,B)`: from Stage 2a.
- Confidence: `c = games_AB / (games_AB + 200)`.
- `Lane(A,B) = (2/3)·c·Stat + (1/3 + (2/3)·(1 − c))·Style`.

### Whole comp (teamfight scope)
```
Overall = Σ over the 3 core pairs of [ 0.50·c·Stat + (0.25 + 0.50·(1 − c))·Style ]
        + Σ over the 7 other same-team pairs of 0.50·Stat
        + 0.25·Team
```
- Equivalently `Overall = 0.75·Σ core Lane + 0.50·Σ other-pair Stat + 0.25·Team` (tested).
- Component totals for display: `Stat_total`, `Style_total`, `Team_weighted = 0.25·Team`; they sum to Overall.
- All weights in config.

### API
`POST /api/synergy/evaluate`: up to 5 champions with roles plus optional enemies. Returns per core pair Stat / Style / Lane / games / confidence / top rules; team Stat_total / Style_total / Team (and vs enemy) / Overall / identity / damage profile / trait bars; for missing roles the top 5 pool champions by Overall gain.

### Stage 3 tests
1. `c = 1`: Lane = (2/3)·Stat + (1/3)·Style. `c = 0`: Lane = Style.
2. The Overall decomposition holds to 1e-9, and the component totals sum to Overall.
3. Banned or picked champions are excluded from suggestions (guardrails).
4. Evaluating a 5-champion comp returns in under 100 ms (pair Style precomputed).

---

## Stage 4: merged Prep + Synergy page, top half

Prep and Synergy become one page, **Prep**; Rules and Tags are linked from its header. Scope toggle (In-lane / Teamfight), View toggle (Overall / Stat / Style / Team), threshold slider; a 5-column visualiser (pools, ≤ 15 per role, layered SVG); a plan panel (locked champions, Stat / Style / Team / Overall, identity, damage bar, trait bars); a sortable synergy table (Combo, Stat, Style, Team, Overall, games, why) whose rows lock into the plan. In-lane scope shows core-pair edges only; Teamfight all cross-role edges at lower opacity, Team in the panel once 3+ roles are locked. Hover shows all components, games and top rules. Locking collapses a column; other nodes show chips with their Overall gain.

Tests: payloads for both scopes and all views; locking updates the panel and chips; 1920×1080 Playwright screenshots with no overflow.

## Stage 5: plan-driven bans and picks, bottom half

For the current plan, side and format (tournament default, Flex toggle):
- **Bans** (top 5 each): **Win lane** (lane ban score with each plan champion the only responder in its role; reason names the champion it protects), **Win teamfights** (`P(enemy picks X) × ΔTeam vs enemy`; reason names the rule), **Combined** (`ban_value` with plan champions as locked, plus `d × teamfight(X)`, `d = 1.0`). All guardrails apply; Flex uses group bans and the enemy-ban discount.
- **Picks:** pick order by side (contest risk − counter risk + flex, mapped onto the side's slots: blue B1 / B2–B3 / B4–B5, red R1–R2 / R3 / R4–R5); fills for the other roles (top 3 by Overall gain, with breakdown); plan risks (top 3 enemy champions and identities that hurt Team vs enemy; a backup per plan champion with the Overall drop).

Tests: Win lane uses only the plan champion; a planted disengage champion ranks first vs an engage plan; pick order respects ban rate vs counter risk on both sides; backups exclude unavailable champions and come from the right pool; guardrail fuzz passes with plans active.

## Stage 6: pin a plan into the live draft

"Pin for next game" saves the plan (champions with roles, pick order per side, ban lists). Live: banner `Plan: … — intact / at risk / broken` (with the backup once broken); plan Combined bans get `plan_ban_bonus = 0.3` log-odds and a `plan` tag; plan champions due at the current slot get `plan_pick_bonus = 0.3` and a `plan` tag; guardrails still win; one-click unpin. The pinned plan is stored on the draft record; History shows "plan followed? yes / partly / no".

Tests: banner states in a replayed draft; bonuses only for plan champions at their slot, guardrails still win; History plan adherence.

## Acceptance (overall)
- `uv run pytest` passes at each stage.
- On Prep: lock a jungle–mid duo and a bot duo and see Stat / Style / Team / Overall, identity and damage profile, the three ban lists, pick order for blue and red, fills for top, plan risks with backups.
- Pinning changes Live suggestions as described in a replayed draft.
- Live still meets its budgets (200 ms single turn, 300 ms Flex group turn).

## Out of scope
Trio-specific win-rate data; roam pairs (JG–SUP, MID–SUP) as separate in-lane scores (they count in Overall through other-pair Stat); external synergy data (Machine LoLing, lolalytics).

---

## Decisions (3 Nov 2026)
1. Dependencies: PyYAML (rule and tag files) and numpy (ridge / logistic fits) added.
2. Tags drafted for every champion in the roles where it is commonly played (171 champions, 247 champion-roles), which covers our pools and the top 30 per role. New releases Claude doesn't know (Locke, Zaahen) are left untagged on purpose.
3. Each stage is committed on branch `claude/synergy-v2` and pushed once reviewed.
4. "Half weight" for unreviewed tags = blend halfway towards the role's average for that tag:
   `0.5 × own + 0.5 × role average` (all 0–1). Halving the value was rejected: it only pulled high tags down,
   made `not X` read unreviewed carries as half non-carries, flipped rankings on review and biased the fits.
5. Live pick scoring keeps the Stat synergy term; Style / Team reach Live only through the Stage 6 plan bonuses. comp-style labels (Dive / Poke / Front to back) are unchanged.

## Built

### Stage 1 (3 Nov 2026)
- **Field mapping:** `data/traits_map.py` (`parse_traits`, `TRAIT_SOURCES`). Checked against a live EUW match: every key present. Share of damage taken comes from `challenges.damageTakenOnTeamPercentage`, else `totalDamageTaken` over the team's. Six new nullable `match_participants` columns, filled by the crawler; `data.backfill` fills older matches from the Riot cache (no API calls). `champions.attack_range` from Data Dragon (re-run `data.ddragon` once).
- **Traits:** `data/traits.py` → `champion_traits` (window, bracket incl. "all"), rebuilt by `aggregate()` with the synergy window. Champion-roles with < 20 games get no percentile traits. Multi-source traits are the mean of their sources' percentiles. Damage shares stay real shares; `ranged` is 0/1. `power_curve` is the percentile of the slope (`power_curve_slope`, logit win rate per bucket, each bucket shrunk towards the champion's own win rate, k = 50, ≥ 50 games).
- **Reading traits:** `engine/traits.py` `ChampionTraits.intensity(champ, role, term)`: tags (scaled /3; unreviewed blended halfway towards the role average), measured traits, derived `ranged_long` (0 at range 500, 1 at 650), `power_curve_late` / `power_curve_early`, and `not X`. A champion's tags in another role are used when its role has none. Unknown = None.
- **Tags:** `data/champion_tags.yaml` (project root `data/`, next to the database; one line per champion-role, written deterministically) ↔ `champion_tags` table, synced at app start. Tags page (`#/tags`, linked from Prep): filters To review / Reviewed / Untagged, 0–3 per tag, "Looks right", measured traits for context; edits write the YAML.

### Stage 2 (3 Nov 2026)
- **Rule files** (project root `data/`, drafted by Claude for review): `style_rules.yaml`, 46 pair rules (15–16 per core pair); `team_rules.yaml`, 14 whole-comp rules + 7 cross-team rules. Terms may be compound (`hypercarry * not dive`). A rule's id is `PAIR:ROLE=term,ROLE=term` in the order written.
- **Style fit** (`engine/style.py`): ridge centred on the priors, prior SD 1pp (`app_settings.synergy_v2.prior_sd_pp`). s_raw is shrunk by c = n / (n + 200), so the strengths are scaled by c and each pair is weighted by the precision of its s_raw (≈ (n + 200)² / 4n). This undoes the Stat shrinkage, so the fit isn't biased towards 0. A rule whose term is unknown for either champion doesn't fire. "Support" = games of pairs where the rule's strength ≥ 0.25.
- **Team model** (`engine/team.py`): features `max`, `mean`, `sum`/`cap`, `lack`/`need`, `excess`/`over`, `protect`/`by`, `all` (product), `special` (`damage_imbalance` from 70% of one type, `curve_coherence`, `identity_coherence`); cross rules = our feature × theirs. Unknown intensities are filled with the role's average (so they cancel against the average comp). MAP logistic fit, blue's view, with an unpenalised side intercept. `Team(comp) = (f(comp) − f(avg comp in the bracket))·β`; `Team vs enemy` adds the cross terms. Identity: engage (engage + ½ dive), poke, pick, protect (enchanter + ½ disengage + ½ peel), split; coherence = share of each champion's strongest identity that points at the label.
- **Across-role traits (decision):** within-role percentiles can't be added across a comp (a top-percentile ADC isn't a frontliner), so `champion_traits` also stores `cc_all`, `frontline_all`, `peel_all` (percentiles across every role). Team rules and the trait bars use them; pair rules keep the within-role traits.
- **Fits** (`data/synergy_model.py` → `synergy_fits`): refitted at the end of every `aggregate()` (a broken rule file is logged; old fits stay) and after a prior edit. Bracket-pooled coefficients; the average comp's features are stored per bracket. A fit made under an older prior is treated as stale (the prior is used until the next fit).
- **Rules page** (`#/rules`, linked from Prep): pair-style and team tabs, prior (editable: writes the YAML, then refits everything), fitted, support, "disagrees" flag (> 1.5pp from the prior or the other sign).
- **Measured** on a scratch database with 15.7k synthetic matches across 171 real champions: aggregation + traits + fits 9 s, a refit 2.3 s. With SD 1pp the prior is strong: recovering a planted −4pp against a −1pp prior took ~100k matches in the test (30k got −2.5pp); pair rules on tags barely move on one patch of pair data (most core pairs have a handful of games), as intended.

### Stage 3 (3 Nov 2026)
- `engine/synergy_v2.py`: `SynergyEvaluator` (pair, evaluate, suggest, warm), `SynergyWeights` (Settings). Generalised weights: a core pair contributes `ws·c·Stat + (wy + ws·(1 − c))·Style`, Lane is that over `ws + wy`, other pairs `ws·Stat`, plus `wt·Team`; defaults 0.50 / 0.25 / 0.25 and k = 200 give the spec's formulas (decomposition tested to 1e-9).
- `POST /api/synergy/evaluate` (`api/synergy_v2.py` for the JSON): champions `[{champ_id, role}]`, optional `enemies` (`role` optional), `banned`, `k`. Returns pairs (Stat / Style / Lane / Overall part, games, confidence, top rules), team (Stat_total / Style_total / Team / Team_weighted / Overall, Team vs enemy with its top rules, identity, damage, bars) and suggestions for the missing roles.
- Settings page: a "Synergy (Prep page)" section (Stat / Style / Team weights, confidence k, rule prior SD — saving it refits — and d for the Stage 5 Combined bans).
- Measured on the scratch database: building + warming the evaluator 0.12 s; evaluate through the API 12–15 ms.

### Stage 4 (3 Nov 2026)
- `POST /api/prep/synergy` (`api/prep_view.py`): scope, view, threshold, locks, columns, use_draft → five columns (nodes with tier, lock, unavailable, `untagged`, comp-style badge, `gain_pp` chip), edges for the scope and view with every component, the plan panel (`evaluate` of the locks: pairs, totals, Team shown from 3 roles, identity, damage, trait bars, top team rules) and the table (core pairs; from 2 locks the top 10 single-champion completions).
- `web/src/PlanSynergy.svelte` (top half) inside `Prep.svelte`; the plan (locks) is remembered per browser. The old Synergy page, `/api/synergy` and `build_synergy_view` (with their tests) are gone; `#/synergy` opens Prep. The old Prep sheet moved into a collapsed "Patch overview" at the bottom.
- Measured on the scratch database (real pools of 8–10 per role): the page payload with no locks and threshold 0 in Teamfight scope is 0.24 s / 390 kB; with 4 locks 14 ms. Screenshots at 1920×1080: no horizontal overflow.

### Stage 5 (3 Nov 2026)
- `engine/plan.py`: `PlanContext` (engine, synergy evaluator, plan, DraftState), `win_lane_bans`, `win_teamfight_bans`, `combined_bans`, `pick_order` / `our_slots`, `fills`, `plan_risks`, `backups`. `engine.py`: `_ban_candidates` / `_lane_ban_tables` / `ban_group` take an optional `plan` (treated as locked) and `extra` (added to the weighted sum); the defaults leave Live unchanged. `TeamModel.vs_batch` scores one comp against many enemy comps at once.
- `POST /api/prep/plan` (locks, side, format tournament | flex, use_draft) → bans (lane / teamfight / combined, each with reason, value, owner in Flex, components), picks (order with one line each, fills, risks, backups). `web/src/PlanBansPicks.svelte` under the top half, on the same plan.
- Measured on the scratch database (4 locks): 0.37 s cold, ~0.08 s warm, tournament and Flex. Live suggestion budgets are untouched (the Live path calls the engine with no plan).

### Stage 6 (3 Nov 2026)
- `engine/plan.py`: `build_pin` (the plan, its pick order and Combined bans for both sides and both formats, backups, bonuses) and `pin_effects` (banner state, ban bonus, pick bonus for a DraftState). `engine.py`: `suggest_bans` / `ban_group` / `suggest_picks` take an optional `bonus`; `Suggestion.plan` marks a bonus. Parser: `LiveDraft.enemy_hovers` (champions in the enemy's in-progress ban or pick action).
- API: `GET / POST / DELETE /api/plan/pin`; the pin is kept in `app_settings` (`pinned_plan`), so it survives a restart. Live payload: `plan` (banner text, state, at-risk and gone champions with backups); cards carry `plan`. `drafts.plan_json` stores the plan pinned when the champ select started; History rows and recaps show "plan followed? yes / partly / no".
- UI: "Pin for next game" / "Unpin" on Prep (bottom half header); `plan` tags on cards; History chip and recap line. Settings: plan ban / pick bonus.
- **Plan bar** (after review, replacing the thin top banner): fixed at the bottom of the Live screen, while waiting and during champ select (`web/src/PlanBar.svelte`, payload `plan` from `api/live.py`):
  - state (intact / at risk / broken) and Unpin;
  - each planned champion (icon, name, role) with its status against the draft: planned, picked ✓, replaced → what we picked in that role instead, banned / taken by them → its backup, at risk;
  - bans: the plan's Combined bans for this side and format (banned by us, banned by them, picked, not banned yet) and our off-plan bans;
  - summaries for the **plan** and for **now** (our picks + the plan's remaining champions, or their backups once gone): damage split (AD / AP / true), comp timing (Early / Mix / Late) and fight style (Front to back / Dive / Poke, comp-style labels).
  - It fits the Live screen at 1920×1080 at its fullest (5 cards + Next 2 picks), with no scroll.
- Budgets with a pinned plan (full-size synthetic data, tested): single ban turn < 200 ms, Flex group turn < 300 ms, pick turn < 200 ms.

## Decision log (Stages 3–6, built without review: please check these)

Each entry: the question, the options, what I chose and why.

**S3-1. Team for a partial comp.** Options: (a) Team only once all 5 roles are known; (b) only from 3 locked roles, 0 before; (c) fill missing roles with an average champion of the role, always computed. **Chose (c)** for the numbers (so "Overall gain if added" is well defined at every step, and Team moves smoothly as locks are added), and (b) for display: the plan panel shows Team from 3 locked roles, as the spec says (`show_team`). With nothing locked Team is ~0 by construction.

**S3-2. Team vs enemy with a partial enemy comp.** Same fill: the enemy's missing roles are average champions, so cross terms with an unknown enemy are neutral.

**S3-3. Enemy roles when not given.** Options: ask for roles; most likely assignment from pick rates; best-for-them assignment. **Chose the most likely assignment** (the engine's existing enemy role model; CLAUDE.md "Lessons learned" rules out the best-for-them one).

**S3-4. Where pair Style is precomputed.** Options: a nightly table in the database; in memory when the evaluator is built. **Chose in memory**: fits only change at aggregation (or a tag / rule edit), and the evaluator is rebuilt then (`Services.invalidate_synergy`), warming every core pair of our pools in 0.1 s. Same effect as a nightly table, no extra table to keep in step.

**S3-5. Which Stat table.** The engine's (the synergy bracket setting, default "all"), and the average comp for Team from the same bracket. Alternative: each player's own bracket per pair — rejected, pairs cross players with different brackets.

**S3-6. Suggestions for missing roles.** Pool champions only (as specified), ranked by Overall gain including Team (filled). Excluded: `banned`, enemy champions, our own comp; then checked by `guards.validate_suggestions` against a provisional `DraftState` so availability still has one source of truth.

**S3-7. Settings scope.** The weights apply to the Prep page and its plan (Stages 4–5). The Live engine's v1 synergy term and its damage-balance penalty are unchanged (decision 5 above); the Team model's `damage_imbalance` rule is the v2 replacement on Prep.

**S4-1. What "Overall" means for an edge.** In-lane scope: the pair's **Lane** score. Teamfight scope: the pair's **part of Overall** (`0.50·c·Stat + (0.25 + 0.50·(1 − c))·Style` for core pairs, `0.50·Stat` for the others), so edges sum to the plan's Stat + Style. Alternative: always Lane (undefined for non-core pairs).

**S4-2. The threshold slider.** Options: keep the old trio threshold; filter edges by |value|. **Chose edges by |value|** in the selected view (default 0.5pp, matching the old "hide within ±0.5pp"). Trios are dropped from the page: trio-specific data is out of scope, and the plan panel already scores any 3+ champions with pairs plus Team.

**S4-3. The old Synergy page.** Options: keep it alongside; redirect; delete. **Deleted** the page, `/api/synergy`, `build_synergy_view` and their tests (`tests/test_synergy_view.py`), since the spec replaces them. The draft-context and column helpers stay (`api/synergy.py`), with their tests moved to `tests/test_prep_page.py`. `#/synergy` redirects to Prep.

**S4-4. The old Prep sheet** (priority bans per side, best 3, blind-safe and flex picks, lane ban tables). Options: delete; replace with Stage 5; keep. **Kept, collapsed** under "Patch overview" at the bottom: it answers "no plan yet" questions that Stage 5 doesn't.

**S4-5. Completion rows.** "Top completions of the plan" = the best single champion added in any open role (top 10 by Overall gain), each with the full comp's Stat / Style / Team / Overall and the strongest new rule as "why". Alternative: completing all open roles at once (combinatorial; Stage 5's "fill" covers the per-role view).

**S4-6. Where the plan lives.** In the browser (localStorage, like the old visualiser's locks); Stage 6 (pinning) would store it server-side. Clicking a pair row locks both champions; a completion row locks the one champion.

**S4-7. Screenshots.** Taken with Playwright against a scratch database (a script, not a pytest test: the project has no Python Playwright dependency, and screenshots stay local in `tests/screenshots/`).

**S5-1. P(enemy picks X) for Win teamfights.** Options: the engine's next-pick probability (Threat); X's per-team pick rate. **Chose the per-team pick rate given it isn't banned**: `Σ_roles pick_rate / 2 / (1 − ban_rate)` (pick rates count both teams), capped at 95%. The plan is about the whole draft, not the next pick. Champions under 0.2% are skipped.

**S5-2. ΔTeam for a candidate.** Team vs enemy with X in an average enemy comp (missing roles = role averages; revealed enemy picks kept), minus without it, averaged over X's open roles by role share (roles ≥ 10% of its games). Reason = the rule X hurts us most through, with its pp.

**S5-3. Our own plan champions are never banned** in any list (like declared intents in Flex). Own-pool bans of non-plan champions stay allowed (ban-guardrails).

**S5-4. Win lane value.** `0.04 × TeamLaneBan` × relevance (× the enemy-ban discount in Flex): the Matchup component on its own, so the list reads "what protects our lanes". Reason names the plan champion it protects, "worst common lane" when it is the lowest Resp among the role's 10 most picked opponents, "no matchup data" when Resp is the no-data baseline.

**S5-5. Combined.** The engine's ban value with the plan as locked: Matchup uses plan champions as the only responders; Comp counts counters to plan champions (weighted by the enemy lane still being open); the self-ban penalty skips planned roles. Plus `d × Win teamfights` inside the weighted sum (so relevance and the Flex discount apply), d = 1.0 (Settings). The reason switches to the teamfight rule when that term is the largest. Flex: the engine's greedy group bans, one per teammate (owner shown).

**S5-6. Pick order: how the spec's "contest − counter + flex" is put into one unit.** Everything in pp of win chance, per slot:
- contest(slot) = 1 − (1 − q·share of enemy bans before the slot)(1 − P(enemy picks it)·enemy picks before / 5), q = P(the enemy team bans it) from the ban rate;
- cost of losing it = 25 × (engine pick weight over its backup + Overall drop to the backup), floored at 0; 5pp if the pool has no backup;
- counter cost = the engine's own counter-risk weighting: 100 × risk_weight × best counter's lane logit × min(1, enemy picks after / risk_horizon);
- flex = 100 × flex_bonus × (roles − 1) × enemy picks after / 5.
The assignment of plan champions to our open slots is chosen by brute force (≤ 120 permutations) to minimise contest × cost + counter − flex. "Priority" shown = contest at our last slot × cost − counter at our first slot + flex. Alternative considered: rank by priority and fill slots in order (ignores that waiting lowers counter risk and raises contest at different rates per champion).

**S5-7. Slot groups.** Our picks with no enemy pick between them, and the last pick phase as one group: tournament blue B1 / B2–B3 / B4–B5, red R1–R2 / R3 / R4–R5 (as in the spec); Flex blue B1 / B2–B3 / B4–B5, red R1–R2 / R3–R4 / R5. Lines: first group "B1 X: contested 18%, safe blind" (or who counters it), held picks "Hold X to B4/B5: countered by Y (−3.4pp)".

**S5-8. Plan risks: enemy comp identities.** For each identity, an enemy comp of real champions: per open role, the enemy's 12 most-picked champions, the one contributing most to that identity. Shown when it lowers Team vs enemy. Alternative: synthetic "pure identity" comps (not real drafts).

**S5-9. Backups.** The player's pool champion that keeps Overall highest (Stat + Style + Team), never banned, picked or another plan champion; shown as the Overall change (it can be positive: then the backup scores better than the plan champion on synergy alone).

**S5-10. Format default and side.** Tournament (Ranked 5s) by default, with a Flex toggle; side toggle blue / red; both remembered per browser. The bottom half follows the top half's plan, including champions locked in a live draft when "use current draft" is on.

**S6-1. Banner states.** broken = a plan champion is gone (banned by either team, or picked by the enemy); the banner shows that role's backup (stored with the pin). at risk = an enemy in-progress ban or pick action shows a plan champion (the parser now reads it). Our own pick of a plan champion keeps the plan intact. Alternative considered: "at risk" also when a plan champion's contest risk is high (left out: it would be amber before every game).

**S6-2. "Due at this slot".** A plan champion gets the pick bonus at every pick of the slot group it was assigned to, and later (if we haven't picked it yet), because the two picks of a double turn lock in either order. It gets none before its group, none once its role is filled by someone else, and the guardrails still drop it when it is gone.

**S6-3. Ban bonus only for useful bans.** The bonus applies only to plan bans whose own value is already > 0: a ban that hurts us isn't turned into a suggestion because the plan listed it (Rule 3 of the guardrails stays meaningful). Units: ban values are log-odds, so +0.3 is added as is; pick values are in win-probability units, so the 0.3 log-odds is converted with the slope at 50% (× 0.25 = +7.5pp of value). Observation from the demo: on synthetic data 0.3 is large next to ban values (~0.05–0.3), so plan bans filled 4 of 5 cards at our first ban turn; lower it in Settings if that's too strong on real data.

**S6-4. What a pin stores.** The pin is computed when you press it, for both sides and both formats (side and format are only known in champ select): pick orders, Combined bans, backups, and the bonuses then set in Settings. A draft keeps its own copy (`plan_json`), so unpinning or re-pinning later doesn't change History. One pin at a time; it stays pinned until unpinned (a game doesn't use it up). Alternative: auto-unpin after a logged game (left out: a team often plays the same plan twice).

**S6-5. Plan adherence.** yes = every plan champion was picked by us (any role), partly = some, no = none, with "n of m". Bans aren't scored: a ban list is a menu, not a plan.

**S6-6. Unpin from Live** is one click (a DELETE); the banner disappears at once on that screen and from the next champ-select event everywhere.

**S6-7. Plan bar contents.** Timing and fight style use the comp-style labels (`engine/comp.py`: power curves, reviewed Dive / Poke / Front to back weights) so the bar matches the comp panels and badges on the same screen; the synergy v2 identity (engage / poke / pick / protect / split, from tags) stays on Prep. "Now" fills roles we haven't picked with the plan champion while it's available, else its backup, so it shows where the plan is heading, not only what is locked. Damage uses the per-champion damage profile from the crawl (no data → "no damage data").
