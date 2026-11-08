# Bug fix: Flex (standard ranked draft) bans must show all 5 suggestions at once

Read `CLAUDE.md`, `docs/SPEC.md` (Draft engine, LCU), `docs/prompts/ban-score.md` and `docs/prompts/ban-guardrails.md` first. Plan first, show me the plan, then build. Write the failing test first.

## The bug

In Flex (standard ranked draft), all ten players ban **at the same time** in a single ban phase. The app currently treats bans as one-at-a-time turns, so it shows suggestions for one ban only. In this phase the Live screen must show **5 coordinated ban suggestions together, one per teammate**.

Tournament draft (Ranked 5s, Clash) still has one ban per turn. Don't change that behaviour.

## Root cause to check first

In the LCU session, the standard ranked ban phase is a single `actions` group containing every player's ban action at once (several ally actions in progress at once). The parser or turn mapper probably assumes one active ally action per turn. Confirm against a recorded Flex fixture. If none exists, record one with the replay/record tool (or build a fixture from the session schema and mark it synthetic).

## Required behaviour

1. **Group turns.** Generalise the turn model: the active turn can contain N simultaneous ally actions of one type. The number of suggestions shown = the number of our ally ban actions in that turn that are still incomplete.
2. **A coordinated set, not the top-5 of a list.** Choose the 5 bans **greedily with recomputation**:
   - Score all candidates, take the best, treat it as provisionally banned, re-score (the enemy pick distribution and lane ban scores shift once it's gone), take the next, and so on.
   - This avoids five bans that all target the same lane or overlap each other.
3. **Discount bans the enemy will probably make anyway.** Both teams ban blind, so a champion the enemy is likely to ban too is partly wasted. Multiply each candidate's value by `(1 − P(enemy team bans X))`, with `P(enemy team bans X) ≈ 1 − sqrt(1 − ban_rate(X))` from the enemy bracket's `champ_role_stats`. This assumes the two teams ban independently; note that in a code comment.
4. **Assign each ban to a teammate.** Each of the 5 suggestions gets an owner (Top / Jungle / Mid / ADC / Support):
   - The owner is the role whose lane ban score contributes most to that ban. Resolve conflicts with a one-to-one assignment (brute force over 5! = 120 is fine) that maximises total contribution.
   - If a ban has no clear lane owner (a global meta ban), give it to whoever is left.
5. **Teammates' ban choices.** If the session shows a teammate's ban as locked, or hovered where the client exposes it:
   - A locked ban fills that teammate's slot and counts as banned for the rest of the set (Rule 1 of the guardrails). Re-run the greedy selection for the remaining slots.
   - A hovered ban is shown on their card as "hovering X" next to our suggestion. Don't overwrite their slot.
6. **Declared pick intents.** In standard draft, teammates declare their pick intent before bans. Never suggest banning a champion a teammate has declared. This is stricter than the hover rule in tournament draft, because here the intent is a declared plan, not a passing hover. Make it a config flag `ban_declared_intents` (default `false`).
7. **All guardrails still apply:** no banned or picked champions, no duplicates within the set, no ban with value ≤ 0. If fewer than 5 are worth it, show fewer, and label the empty slots "no useful ban — free choice".

## Live screen

During a group ban turn, show a row of **5 cards**, one per teammate, each with:
- the teammate's role and name,
- the suggested champion (icon and name) with its one-line reason,
- the status: `suggested` / `hovering X` / `locked X`.

Keep the 3-card layout for single-action turns.

## Tests

1. A recorded or synthetic Flex fixture: during the ban phase the API returns exactly 5 distinct suggestions, each with a distinct owner role.
2. Greedy recomputation: two candidates that only threaten the same lane aren't both chosen when an equally valuable candidate threatens another lane.
3. Enemy-ban discount: a 50% ban-rate champion has P(enemy team bans) ≈ 0.293, so its value is multiplied by ≈ 0.707; a 0% ban-rate champion's value is unchanged.
4. A teammate locks a ban mid-phase: their slot shows it, and the other 4 are recomputed with no duplicate.
5. A declared pick intent is never suggested when `ban_declared_intents=false`.
6. Tournament-draft fixtures still produce 3 suggestions per single-ban turn (no regression).
7. The guardrail fuzz test from `ban-guardrails.md` passes with group turns included.

## Acceptance

- `uv run pytest` passes.
- Replaying the Flex fixture shows 5 cards on the Live screen during the ban phase, updating as teammates lock bans.
- A suggestion set for a group turn returns in under 300 ms.


## Decision (8 Nov 2026): a mix, at most two per lane

Test 2's open point is settled: greedy recomputation keeps concentrating where we're weakest, but at most
`LANE_BAN_CAP` = 2 group bans go at one enemy lane (the ban's most likely role). Past that, a ban's value flips to a
penalty (`LANE_CLUSTER_PENALTY`), so it's never chosen and the next ban moves to another lane. Test:
`test_at_most_two_bans_per_enemy_lane`.
