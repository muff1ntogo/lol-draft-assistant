> **Superseded by `docs/synergy-v2.md`** (3 Nov 2026): Stage A's pair data and score remain as the **Stat** component; the Synergy page and engine integration are replaced.

# Task: synergy visualiser (inspired by Machine LoLing's Synergy Visualizer)

Read `CLAUDE.md`, `docs/SPEC.md` (Data sources, Data model, Draft engine, UI) and `docs/prompts/ban-value-matchups.md` first. Build it in the three stages below, stopping after each for my review. Plan first, show me the plan, then build. Write the tests for each stage first.

Reference: https://machineloling.com/2026/02/05/synergy-visualizer/. It graphs pairwise and trio synergy across up to three role pools, with a threshold slider and locked picks. Synergy there is a "normalised delta": the duo's win rate with both champions' own strength removed, so only the interaction remains. We are building our own version on our crawled data. Don't scrape their tool.

---

## Stage A: the synergy metric and data

### A1. Collect role-aware duo data

In the match crawler, for every match and both teams, record every **same-team pair** with their roles. That's 10 pairs per team.

New table `synergy_pairs`:
- key: `window`, `bracket`, `role_a`, `champ_a`, `role_b`, `champ_b`
- values: `games`, `wins`
- Store each pair once, ordered by `(role, champ)`.

Also store champion role win rates for the same window, so strength is removed consistently.

**Damage profile.** From `participants`, also aggregate each champion's mean share of `physicalDamageDealtToChampions`, `magicDamageDealtToChampions` and `trueDamageDealtToChampions`, per role. Add these as columns on `champ_role_stats`.

### A2. Pool for sample size

Interaction effects are fairly stable across patches, while champion strength isn't. So synergy uses a **bigger, pooled window** than the meta stats:
- All patches in a rolling 6-month window, with time-decay weight `0.5 ** (patches_ago / 6)`.
- All crawled brackets (Iron, Gold, Emerald) pooled by default, with a setting to use a single bracket.
- Champion strength `p_A` used in the formula comes from the same pooled, weighted window.

### A3. Normalised synergy score (log-odds)

For champion A in role r1 and champion B in role r2 on the same team:

```
e      = logit(p_A) + logit(p_B)                       # expected log-odds if they don't interact
p_obs  = (wins_AB + k · sigmoid(e)) / (games_AB + k)    # shrink towards the expectation, k = 200
s_raw  = logit(p_obs) − e                               # interaction only; shrinks to 0 when data is thin
s      = 0.3 · tanh(s_raw / 0.3)                        # soft cap at about ±7.5pp, so outliers can't dominate
```

- Display it in percentage points as `25 · s` (the slope of the sigmoid at 50% is 1/4).
- `s` is symmetric: `s(A,B) = s(B,A)`.
- Pairs with `games_AB < 30` are stored but shown as `low data` (hatched or dashed in the UI).

### A4. Trio score

```
T(A,B,C) = mean(s_AB, s_AC, s_BC)
```

A trio also carries `weakest_pair = min(s_AB, s_AC, s_BC)`. The UI flags a trio whose mean passes the threshold but whose weakest pair is below `−1.5pp`. Machine LoLing notes that a strong pair can hide a weak one.

### Stage A tests

1. Synthetic data where A and B have no interaction (the duo win rate equals the independence expectation) gives `s ≈ 0`.
2. Strong positive interaction, many games: `s` > 0 and close to `s_raw`. The same interaction with 5 games: shrunk close to 0.
3. Symmetry, and the soft cap: `|25·s| ≤ 7.5` always.
4. Trio mean and weakest pair computed correctly.
5. Damage shares sum to about 1 per champion and role.

---

## Stage B: the Synergy page (visualiser)

A new page, **Synergy**, also reachable from Prep.

### Inputs
- Pick 2 or 3 of our roles (default: Jungle, Mid, Support), and optionally all 5.
- Each role's column defaults to that player's pool from `pool_entries`, with up to 15 champions per role. Add or remove champions by hand.
- A **threshold** slider in pp (default +1.0pp) for which trios are drawn.
- **Lock** a champion in a role (click its node).
- A toggle: "Use current draft". When champ select is live, locked picks and bans come from the current draft, and banned or picked champions are greyed out.

### Graph (layered, not force-directed)
- One **column per selected role**, with champion nodes (Data Dragon icons plus a tier badge: main / comfortable / learning).
- **Pair edges** between champions in different columns: thickness and colour by `25·s` (green positive, red negative, grey within ±0.5pp), and the signed value on hover. Edges whose |value| is below 0.5pp are hidden by default (there's a toggle to show them).
- **Trios** with `T ≥ threshold` are highlighted when you hover a node: every trio through that node lights up. A trio flagged for a weak pair is drawn dashed.
- **Locking** a champion collapses its column to that node. Every other node then shows small chips with its synergy to each locked champion (e.g. `J +2.1` for jungle, `M −0.8` for mid), coloured and signed.
- Render with SVG (d3 or plain SVG in the Svelte/React front end). With 3 × 15 nodes, at most 675 pairs, it must stay smooth.

### Side panel
- **Top trios** in the current selection: champions, `T`, weakest pair, and which players play them.
- **Locked team synergy:** every pair among the locked champions, and their total.
- **Damage profile** of locked plus hovered champions: a stacked physical / magic / true bar, with a warning when one type is above 80%. This covers the gap Machine LoLing's commenters raised (the tool suggesting all-AP or all-AD comps).

### Stage B tests
1. The API returns nodes, pair edges and trios for a fixture pool, and respects the threshold.
2. Locking a node: that column has one node, and every other node has chips for each locked champion.
3. Banned or picked champions are greyed out and excluded from trios when "Use current draft" is on.
4. A Playwright screenshot at 1920×1080 with 3 roles × 15 champions, saved to `tests/screenshots/`, with no overflow.

---

## Stage C: put synergy to work in the draft engine

1. **Replace** the engine's synergy term with the role-aware `s` from Stage A: `Σ s` over our pairs minus `Σ s` over enemy pairs, using each pair's roles from the role assignment.
2. **Damage balance:** a small penalty when our locked plus candidate comp has more than 85% of one damage type. Default `−0.05` log-odds, configurable, and it can be switched off.
3. **Pick-ahead paths.** When we have two picks in a row (e.g. B2/B3, or R1/R2 and R4/R5 in tournament draft), list the top 3 **pick pairs** from our open roles' pools. Rank them by the engine score (which now includes synergy). Show them on the Live screen as "Next 2 picks" under the normal 3 cards, each with its pair synergy.
4. **Reasons:** when synergy is the largest contributor to a pick suggestion, the reason says so, e.g. `"+3.4pp with your locked Nocturne"`.
5. **Ban Comp term (optional, behind a flag):** in ban phase 2, the Comp component from `ban-value-matchups.md` may also count how much a candidate would complete strong synergy with the enemy's locked champions.

### Stage C tests
1. The engine score's synergy term equals the sum of Stage A scores for the assigned role pairs.
2. The damage penalty applies above 85% of one type and not below.
3. Pick-ahead: in a fixture where B2/B3 are ours, it returns 3 distinct pairs from open roles, none of them banned or picked (guardrails apply).
4. Everything still meets the response-time budgets (200 ms single turn, 300 ms group turn).

## Acceptance (overall)

- `uv run pytest` passes at each stage.
- I can open Synergy, choose Jungle / Mid / Support, lock our jungler, and immediately see which mid and support options synergise best with them, plus the damage profile.
- During a replayed draft, the Live screen shows "Next 2 picks" when we have back-to-back picks.

## Out of scope

- Whole-team concepts beyond damage type (engage, waveclear, frontline). We may add these later.
- Using Machine LoLing's or lolalytics' data or endpoints.
