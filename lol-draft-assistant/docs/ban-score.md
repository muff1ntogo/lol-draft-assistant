# Task: implement the lane ban score (inspired by Machine Loling)

Read `CLAUDE.md` and the "Draft engine" and "Data model" sections of `docs/SPEC.md` first. This task adds one new scoring component to the engine and wires it into ban suggestions. Do it after Phase 1 (engine port) is complete. Plan first, show me the plan, then build.

## What it measures

For one of our players in one role, a ban is worth more when the champion it removes is **common** in that role AND is a matchup our player's pool **handles badly**. The score is the exact change in our player's expected matchup quality if that champion is banned.

## Definitions (per role, per our player)

- **Opponents** `X`: every champion seen in the enemy's version of this role on the current patch and stats bracket, excluding champions already banned or picked in this draft.
- **w(X)**: pick rate of X in this role (patch, bracket). Use `champ_role_stats.picks / total games in role`.
  - Config option `adjust_for_bans` (default `true`): use `w(X) = pick_rate / (1 − ban_rate)`, i.e. how often X is played when it's available.
- **W** = Σ w(X) over all opponents still available.
- **Resp(X)** (in percentage points): the best win-rate delta any eligible champion in our player's pool achieves against X in this lane:
  - `Resp(X) = max over pool champ P of [ 100 × (wr_adj(P vs X) − 0.5) + tier_adj(P) ]`
  - `wr_adj` is the shrunk matchup win rate from the `matchups` table (`k = 200`, as in the spec).
  - `tier_adj`: main = 0, comfortable = 0, learning = −2 pp (configurable). Pool champions that are banned or already picked by either team are not eligible.
  - **via(X)** = the pool champion that achieved the max.
  - Config option `delta_baseline`: `"fifty"` (default, as above) or `"champion_avg"` (subtract P's own average win rate in the role instead of 0.5).
- **μ** = Σ w(X)·Resp(X) / W, the expected matchup quality across the opponents still available.

## Formula

```
BanScore(X) = w(X) / (W − w(X)) × (μ − Resp(X))
```

This is exactly μ' − μ, where μ' is the expected matchup quality after removing X and renormalising the remaining pick rates. Positive = banning X helps us. Negative = X is a matchup we like, so banning it hurts us.

Edge cases:
- If `W − w(X) < 1e-6` (X is effectively the only opponent), return `0` and flag it, rather than dividing by zero.
- If our player has no eligible pool champion with matchup data vs X, use `Resp(X) = 100 × (wr_adj(best pool champ in role) − 0.5)` (their baseline, no matchup info) and mark `via` as `"(no matchup data)"`.
- If our player has already locked a champion in this role, Resp(X) uses only that champion (the pool max no longer applies).
- If the enemy has already picked a champion for this role, the role's ban scores are all 0 (nothing left to deny there).

## Team-level ban score

A ban removes a champion from every role, so for each candidate X:

```
TeamLaneBan(X) = Σ over our 5 roles of BanScore_role(X)
```

Most champions only appear meaningfully in one role, so flex champions naturally score across several roles.

## Integration into the engine

- Put this in `src/draftassist/engine/lane_ban.py` as pure functions taking a data-provider interface (so tests can inject fixtures). No database access inside the scoring function.
- Convert pp to log-odds so it is in the same units as the rest of the engine: `lift_logodds = 0.04 × TeamLaneBan(X)` (the slope of logit at 50% is 4 per unit probability).
- New ban value:
  ```
  ban_value(X) = P(enemy picks X) × harm(X) + γ × lift_logodds(X)
  ```
  with `γ = 1.0` (configurable). This **replaces** the old `0.15 × protect(X)` term in the prototype; the lane ban score is the formal version of "protect".
- Phase 2 bans in tournament draft: only roles the enemy has not yet filled contribute (see edge cases above).
- The one-line reason for a ban suggestion should use this when it is the largest contributor, e.g. `"Common vs our top (PR 12%); our best answer K'Sante only −3.1pp"`.

## UI / output

Add a per-role table to the Prep page (and expose it via the API) with these columns, sorted by Ban descending:

| Opp | PR | Resp | via | Ban |
| --- | --- | --- | --- | --- |
| opponent champion | pick rate, % | best pp delta (signed, 1 dp) | pool champion delivering Resp | ban score (signed, 2 dp) |

Show μ for the role above the table. Colour Ban green when positive, grey near zero, red when negative.

## Tests (write these first)

1. **Worked example**: one role, three opponents, W = 1.0.
   - A: w = 0.3, Resp = −2.0 · B: w = 0.2, Resp = +3.0 · C: w = 0.5, Resp = 0.0
   - μ = 0.0. Expected: BanScore(A) = +0.857, BanScore(B) = −0.750, BanScore(C) = 0.000 (to 3 dp).
   - Also check BanScore(A) equals μ' − μ, computed by removing A and recomputing μ directly.
2. Banned/picked champions are excluded from both opponents and pool eligibility.
3. `W − w(X) ≈ 0` returns 0 with a flag, not an error.
4. A locked pick in our role makes Resp use only that champion.
5. The enemy filling a role zeroes that role's contribution.
6. TeamLaneBan sums correctly for a flex champion present in two roles.
7. `adjust_for_bans` and `delta_baseline` options change results in the expected direction.
8. Property test (hypothesis or random seeds): for random w and Resp, BanScore(X) always equals μ' − μ to 1e-9.

## Acceptance

- All tests pass with `uv run pytest`.
- On the Phase 1 simulated draft, ban suggestions still return in under 200 ms.
- The Prep page (or the API endpoint, if the UI isn't built yet) shows the Opp / PR / Resp / via / Ban table for each of our five roles.

## Out of scope

- Do not change pick scoring.
- Do not fetch new data sources; if the `matchups` table is empty (before Phase 3), use the fixture provider and say so.
