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
4. Live pick scoring keeps the Stat synergy term; Style / Team reach Live only through the Stage 6 plan bonuses. comp-style labels (Dive / Poke / Front to back) are unchanged.

## Built

### Stage 1 (3 Nov 2026)
- **Field mapping:** `data/traits_map.py` (`parse_traits`, `TRAIT_SOURCES`). Checked against a live EUW match: every key present. Share of damage taken comes from `challenges.damageTakenOnTeamPercentage`, else `totalDamageTaken` over the team's. Six new nullable `match_participants` columns, filled by the crawler; `data.backfill` fills older matches from the Riot cache (no API calls). `champions.attack_range` from Data Dragon (re-run `data.ddragon` once).
- **Traits:** `data/traits.py` → `champion_traits` (window, bracket incl. "all"), rebuilt by `aggregate()` with the synergy window. Champion-roles with < 20 games get no percentile traits. Multi-source traits are the mean of their sources' percentiles. Damage shares stay real shares; `ranged` is 0/1. `power_curve` is the percentile of the slope (`power_curve_slope`, logit win rate per bucket, each bucket shrunk towards the champion's own win rate, k = 50, ≥ 50 games).
- **Reading traits:** `engine/traits.py` `ChampionTraits.intensity(champ, role, term)`: tags (scaled /3, × 0.5 when unreviewed), measured traits, derived `ranged_long` (0 at range 500, 1 at 650), `power_curve_late` / `power_curve_early`, and `not X`. A champion's tags in another role are used when its role has none. Unknown = None.
- **Tags:** `data/champion_tags.yaml` (project root `data/`, next to the database; one line per champion-role, written deterministically) ↔ `champion_tags` table, synced at app start. Tags page (`#/tags`, linked from Prep): filters To review / Reviewed / Untagged, 0–3 per tag, "Looks right", measured traits for context; edits write the YAML.
