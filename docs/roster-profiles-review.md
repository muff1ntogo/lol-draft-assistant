# Roster profiles: implementation review and handoff — 12 Nov 2026

Read `CLAUDE.md`, `docs/player-roster.md`, and `docs/roster-profiles.md` before continuing. This document records the
review of Roster Profiles Stages C–E on branch `claude/sharp-noether-n5ljtx` after the Stage B handoff.

## Delivered in this change

| Area | State |
| --- | --- |
| Roster summary API | Built: `GET /api/roster` supplies five ordered lineup slots and enriched bench/player pool cards. |
| Profile API | Built: `GET /api/players/{riot_id}/profile?window=20\|60d`; invalid windows return 422. |
| Bench and activate | Built: named active players can move to the bench; activation restores the selected player's pool and records `active_since`. Activation accepts `{"role":"top"}`. |
| Delete | Built: confirmation must repeat the Riot ID; pools and fetched games are removed; `source="logged"` team-history games remain. |
| Roster persistence | Built: actions call the roster save/reload path, so `roster.json` follows changes. |
| Roster UI | Replaced `web/src/Pools.svelte` with lineup, bench, profile sheet, swap sheet, delete confirmation, and add-player/onboarding workflow. |
| Targeted tests | Passing; see [Validation](#validation). |

### Important data contract

`GET /api/roster` returns a player's pool as an object, not a list:

```json
{
  "pool": {
    "champs": [{"champ_id": 68, "champ": {"name": "Rumble"}}],
    "more": 0,
    "size": 1
  }
}
```

Consumers must use `pool.champs`, and each entry now has its Data Dragon card at `entry.champ`.

## Review: follow-up work

### Bugs / missing acceptance work

1. **Activation undo is not implemented in the UI.** `POST /api/players/{riot_id}/activate` returns an `undo` object when
   it displaced an existing player, but `Pools.svelte` only shows a success note. Add a transient undo toast/action that
   activates `undo.riot_id` in `undo.role`, and test the round trip.
2. **The Add Player sheet skips the lookup step.** It posts directly to `/api/players/onboard`; it does not call
   `/api/players/lookup` to canonicalize the Riot ID, show whether the player was found, display level/rank before
   onboarding, or clearly expose the `{ "no_key": true }` state. Complete the specified lookup/review experience before
   considering Stage D fully accepted.
3. **Manual browser acceptance is still required.** Use a scratch database and verify: empty role; no-key add flow;
   lookup/onboarding and manual pool entry; bench; swap; profile window toggle; delete confirmation; and narrow-screen
   layout. Do not use real player data in committed screenshots or fixtures.

### Cleanup / quality work

1. Add frontend/component or browser coverage for the roster page. Current tests cover API/data contracts, not the
   Svelte handlers, sheets, polling cancellation, or visible error states.
2. Split the compact `Pools.svelte` markup and style rules into readable lines/components if further UI work is planned.
   Keep its existing Svelte 5 rune/event-handler conventions and do not introduce a dependency without approval.
3. Consider an explicit API test for `GET /api/roster`'s empty-role response and all five role positions. The current
   test proves a populated top slot and enriched card only.
4. Confirm real profile response shapes with a safe local dataset, especially rank queues, `profile.window`, profile
   pool entries, and absent-data states. The UI currently tolerates missing values, but this was not manually inspected.
5. The frontend build emits an existing accessibility warning in `web/src/ui/Segmented.svelte`: the element with
   `role="radiogroup"` lacks `tabindex`. It does not fail the build and is unrelated to this change, but should be fixed
   in a focused accessibility cleanup.

## Implementation notes

- `Player.active_since` is timezone-aware UTC and is copied when a placeholder player is re-keyed to a real PUUID.
- Deleting an active player deliberately leaves an empty active role record: its Riot/API identity and pool are cleared,
  while its role slot remains available for a later activation/add.
- `delete_player` removes `PlayerGame` rows except `source == "logged"`. This is deliberate: logged team History rows
  must retain historical ownership/name.
- `swap_player` already supports an empty role. The route issue found during validation was FastAPI body parsing; it is
  fixed with `Body(..., embed=True)`, matching the UI payload `{ "role": "top" }`.
- No migration file was added: schema initialization already handles additive columns for older local databases. Verify
  that path on a copied local database before release.

## Validation

Run after this implementation:

```powershell
uv run python -m compileall -q src
uv run pytest tests/test_roster.py tests/test_profile.py tests/test_pool_proposal.py -q
Set-Location web; npm run build
git diff --check
```

Result on 12 Nov 2026:

- Python compile check: passed.
- Focused tests: **32 passed** (one `starlette.testclient` deprecation warning).
- Vite production build: passed.
- `git diff --check`: passed.
- Full `uv run pytest` was **not** run in this session; run it before merging/releasing and investigate or document any
  unrelated simulation-performance failure.

The Vite build warning above remains: `web/src/ui/Segmented.svelte` needs a `tabindex` for its radiogroup.

## Suggested continuation order

1. Implement and test the lookup/no-key UI state, then activation undo.
2. Run the manual roster acceptance pass on a scratch database.
3. Add frontend/browser coverage for the successful and failed page flows.
4. Run the full Python suite, then resolve or document unrelated failures before merge.

## Repository safety

- Do not commit `.env`, databases, Riot keys, real Riot IDs/PUUIDs, raw cache/timeline data, or screenshots containing
  identity data.
- League client integration remains read-only.
- This is a feature branch, not `main`; its remote is `origin/claude/sharp-noether-n5ljtx`.