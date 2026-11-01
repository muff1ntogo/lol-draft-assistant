# Task: make pool matchup favourability a first-class part of ban value

Read `CLAUDE.md`, `docs/SPEC.md` (Draft engine), `docs/prompts/ban-score.md`, `docs/prompts/ban-guardrails.md` and `docs/prompts/flex-simultaneous-bans.md` first. Plan first, show me the plan, then build. Write the failing tests first.

## Problem

Ban suggestions lean too much on "how likely is the enemy to pick X". They must weigh equally, or more, **how badly our players' pools match up against X**.

- A moderately picked champion that our pool can't answer is a better ban than a popular one our pool counters.
- Banning a champion our pool counters should be actively discouraged.

## Step 1: audit (report back before changing anything)

1. Show me where ban value is computed today and every term that feeds into it.
2. Confirm whether the lane ban score from `ban-score.md` (`TeamLaneBan`, converted to log-odds) is actually wired in, and at what weight.
3. On the Phase 1 simulated draft, print the top 10 ban candidates with each term's contribution, so we can see what dominates.

## Step 2: decompose ban value into three named components

All three are in log-odds units, so they can be added:

1. **Threat (meta):** `P(enemy picks X) × strength(X)`, where `strength(X)` = logit of X's bracket win rate in the roles the enemy still has open. This is independent of our pools.
2. **Matchup (our pools):** `0.04 × TeamLaneBan(X)`, from `ban-score.md`. It is the expected improvement in our players' lane matchups, from their own pools, if X is banned.
   - It is **signed**: if our pools counter X, it is negative and *lowers* the ban value. Don't clip it at 0.
   - Resp uses each player's **eligible** pool champions only (not banned or picked; locked pick only once the player has locked).
3. **Comp (counters to what we've locked):** in tournament draft ban phase 2, how much X counters or out-synergises the champions we've already picked (`Σ` over our locked picks of the matchup or counter logit of X vs them). It is 0 before we've picked anything.

```
ban_value(X) = relevance(X) × (1 − P(enemy team bans X))
               × ( a × Threat(X) + b × Matchup(X) + c × Comp(X) )
```

- `relevance` comes from `ban-guardrails.md`, and the enemy-ban discount from `flex-simultaneous-bans.md` (Flex only; it is 1 in tournament draft).
- Defaults: `a = 1.0`, `b = 1.5`, `c = 1.0`, all configurable.
- This replaces the old `P(enemy picks X) × harm(X) + γ × lift` expression. `harm` is now split into Threat and Comp, so nothing is counted twice.

## Step 3: settings and transparency

- **Settings:** a single slider "Meta threat ↔ Our matchups" that moves `a` and `b` in opposite directions (`a + b = 2.5`), plus an advanced panel with `a`, `b` and `c` as numbers.
- **API:** every ban suggestion includes its three component values and the relevance and discount multipliers.
- **Live screen card:** the one-line reason comes from the largest positive component:
  - Matchup: `"Our top's best answer is −6.2pp (Ornn)"`
  - Threat: `"Top-3 meta jungler, 14% likely"`
  - Comp: `"Hard counters our locked Jinx"`
- Under the reason, a thin 3-segment bar showing each component's share of the total, with a text tooltip.
- **Prep page:** the per-role Opp / PR / Resp / via / Ban table gets an extra column, "Team ban value", so the lane view and the final ranking can be compared.

## Tests

1. **Matchup beats popularity.** Top role, opponents:
   - X1: PR 12%, Resp +4.0pp, win rate 52%
   - X2: PR 6%, Resp −8.0pp, win rate 51%
   - the remaining 82% of pick rate at Resp 0

   With default weights, X2 must rank above X1, and X1's Matchup component must be negative. (Expected lane ban scores: X1 ≈ −0.545pp, X2 ≈ +0.511pp.)
2. **Countered champion is discouraged.** A champion every one of our players counters (Resp > +3pp wherever it's played) gets ban value ≤ 0 and isn't shown (Rule 3 of the guardrails).
3. **Slider extremes.** At full "Meta threat", the ranking matches Threat alone. At full "Our matchups", it matches Matchup alone.
4. **Eligibility.** When a pool champion is banned, Resp for that role is recomputed without it, and ban values update.
5. **Comp is 0 before our first pick** and positive for a known counter after it.
6. **Component sum.** The three components, times their weights and multipliers, equal the reported ban value to 1e-9.
7. All existing guardrail, fuzz and Flex group-ban tests still pass.

## Acceptance

- `uv run pytest` passes.
- The audit output (Step 1) and the new top-10 breakdown (after the change) are both printed for me, on the same simulated draft, so I can compare them.
- Ban suggestions still return in under 200 ms (single turn) or 300 ms (Flex group turn).
