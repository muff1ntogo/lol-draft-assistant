# Enemy pools and likely lanes (8 Nov 2026)

Second task from `ui-engine-review.md`. Built in one stage. Live's enemy column now says what we assume about them and
what they're likely to pick, so you don't look it up elsewhere.

## What changed

- **Role chances for flex picks.** `DraftEngine.their_role_probs(state)`: per locked enemy champion, P(role) as the
  softmax marginal over every assignment of their free roles (the same log role-share model that picks the assignment
  the suggestions use; `role_filled_probs` is its per-role sum). The client's positions (Ranked 5s) are certain.
  Live shows the top two under the name ("Mid 62% / Top 31%") when the top one is under 85% (`ROLE_SURE`).
- **Likely picks per open slot.** `DraftEngine.likely_by_role(state, k)`: per open enemy role, the k champions their
  player there most likely picks (within the role), from the same model as their likely picks (`_enemy_per_role`,
  split out of `_enemy_role_probs`): bracket meta, blended with the scouted player's pool. Each empty enemy slot takes
  an open role (the client's position first, then role order) and shows three faded icons with their chance.
- **Scouted player labels** (`data/scout.scout_summary`): `one-trick` (one champion is 60%+ of 8+ recent games in
  the role), `mastery only` (fetched, no recent ranked games), and `role? 70%` when the role is guessed with under 85%
  certainty (`guess_roles` returns the certainty `assign_roles` already computed).
- **Ban targets.** Scouted ban cards say whose champion they take: "Jungle Name: 8 games (33% of theirs), 50% won",
  or "#2 by mastery" (`_ban_target` in `api/live.py`).

Flex shows no enemy identities, so there the likely picks are meta only and no player labels appear.

## Tests

`tests/test_enemy_lanes.py` (role chances, a known role pushing a flex pick to its other role, client positions,
likely picks, labels) and three Live tests at the end of `tests/test_live_scouting.py`.
