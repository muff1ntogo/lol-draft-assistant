# Task: enforce legal, non-wasted ban suggestions

Read `CLAUDE.md`, the "Draft engine" section of `docs/SPEC.md`, and `docs/prompts/ban-score.md` first. This task adds hard guarantees on what the ban suggestions may contain. Plan first, show me the plan, then build. Write the tests before the code.

## The rules

### Rule 1: never suggest an unavailable champion (hard rule, zero exceptions)

A ban suggestion must never contain a champion that is:
- already **banned** by either team (any completed ban action), or
- already **picked** (locked in) by either team.

The same guard applies to every list the engine produces: our pick suggestions and the "likely enemy picks" list too. They all have the same failure mode.

**Allowed on purpose:** suggesting a ban on a champion from **our own pool** is fine. It's a legitimate way of denying a strong pick to the enemy. Don't filter those out.

**Teammate hovers** (pick intent / incomplete pick actions on our side) are *not* picks. A hovered champion may still be suggested as a ban, but its card must carry a warning tag, `⚠ hovered by <role>`. The client also warns when you ban a teammate's declared pick.

### Rule 2: never waste a ban on a role the enemy has already filled

A ban only has value if it removes something the enemy could still pick. Once the enemy has locked champions for some roles, a champion that only plays those roles is a wasted ban, even if it is strong.

Example: in ban phase 2, the enemy has locked a mid and a jungler. Banning a mid-only or jungle-only champion achieves nothing, so it must not appear.

How to decide:
- Use the enemy's revealed picks to work out which roles are filled. Picks reveal champions, not roles, so use the engine's role assignment. Turn the scores of all permutation assignments into probabilities with a softmax, so you get `P(role r is filled)` for each enemy role.
- For each ban candidate X, compute its **relevance**: `relevance(X) = Σ over roles r of P(X is played in r) × (1 − P(r is filled))`. Here `P(X is played in r)` is X's role distribution from `champ_role_stats` for the enemy bracket.
- **Exclude X if `relevance(X) < 0.10`** (configurable as `min_ban_relevance`).
- Multiply X's whole ban value by `relevance(X)` before ranking. A flex champion whose other roles are still open keeps proportional value. (For example, Taliyah with the enemy mid filled keeps her jungle share.)
- The lane ban score from `ban-score.md` already zeroes filled roles. This rule covers the whole ban value, including the `P(enemy picks X) × harm(X)` term.
- Our own filled roles do **not** make a ban wasted. Banning a counter to our locked mid, while the enemy mid is still open, is exactly what phase 2 bans are for.

Standard ranked draft (bans before any picks) never triggers Rule 2. It matters in tournament draft ban phase 2 (Ranked 5s, Clash).

### Rule 3: a clean top-3

- The three suggestions are three distinct champions.
- If fewer than three candidates pass Rules 1 and 2, show fewer. Never pad the list with invalid or zero-value entries.
- A candidate whose ban value is ≤ 0 after Rule 2 is not shown. A ban that hurts us is not a suggestion.

## Implementation

- **One source of truth for availability.** Add `DraftState.unavailable() -> set[ChampId]`, built from completed ban and pick actions of both teams (ignore `championId == 0`, which is a skipped ban). Every scorer filters candidates with it. Don't re-derive availability anywhere else.
- **Defence in depth.** Add `engine/guards.py` with `validate_suggestions(state, kind, suggestions)`. Call it as the last step before any suggestion list leaves the engine (API and WebSocket push).
  - It re-checks Rules 1–3 independently of the scorers.
  - In tests and dev mode (`DRAFTASSIST_STRICT=1`) a violation **raises**.
  - In normal mode it **drops** the offending entry and logs a warning with the draft state, so bugs are visible but the shot-caller never sees a bad suggestion mid-draft.
- **Recompute on every champ select event.** A champion locked a moment ago must disappear from the next push. Don't cache suggestions across session updates.
- Expose `relevance` on each ban suggestion in the API payload (useful for debugging), but not on the Live screen card.

## Tests

1. **Banned/picked never suggested:** build states where X is banned (by us, by them), picked (by us, by them). X never appears in ban, pick or enemy-likely lists.
2. **Skipped bans:** a completed ban with `championId = 0` doesn't break availability.
3. **Own-pool ban allowed:** a champion in our pool, not banned or picked, can be suggested.
4. **Hover:** a teammate-hovered champion can appear, and carries the `hovered` warning tag.
5. **Role waste:** tournament draft phase 2, enemy has locked a clear mid and a clear jungler. No mid-only or jungle-only champion appears in the ban list.
6. **Flex survives:** with the enemy mid filled, a mid/jungle flex champion is still eligible, at reduced value, while the enemy jungle is open, and is excluded once both are filled.
7. **Ambiguous enemy flex pick:** the enemy locks a mid/jungle flex. Neither role is treated as fully filled, and relevance reflects the split.
8. **Our filled roles don't block bans:** our mid is locked, the enemy mid is open, and a counter to our mid can still be suggested.
9. **Fewer than three:** when only one candidate is valid, exactly one is returned.
10. **Guard raises in strict mode:** feed `validate_suggestions` a list containing a banned champion with `DRAFTASSIST_STRICT=1`, and it raises.
11. **Fuzz:** 10,000 random legal draft states across both formats (seeded). Zero Rule 1 violations, zero suggestions with relevance < `min_ban_relevance`, no duplicates.
12. **LCU replay:** run the recorded fixtures in `fixtures/` through the full pipeline. No violations at any event.

## Acceptance

- All tests pass with `uv run pytest`, including the fuzz test.
- The Phase 1 simulated draft still returns each suggestion set in under 200 ms.
- A grep for any availability logic outside `DraftState.unavailable()` comes up empty.

## Out of scope

- Don't change pick scoring weights or the ban-score formula. Only filter and scale as described.
- Don't write to the League client (as always).
