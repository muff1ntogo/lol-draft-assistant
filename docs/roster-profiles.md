# Task: Roster rework: player profiles, adding players, bench and delete

Asked for on 12 Nov 2026, as part of the UI redesign (`docs/ui-redesign.md`, which this replaces as its step 5). To be
built by Fable, stage by stage, stopping after each for review. Builds on `docs/player-roster.md` (one active player
per role, the bench, `roster.json`); read that first.

> "There should be a player profile function (allowing for analysis of players in the roster rotation, stats, champ
> pool, etc) and adding players (with onboarding steps such as Riot ID + automatic player pool recognition with manual
> additions available afterwards) and removing players / swapping players out should be simple and easy."

## Decisions (user, 12 Nov 2026)

| # | Question | Answer |
| --- | --- | --- |
| 1 | Who builds | Spec now; Fable builds |
| 2 | What a profile shows | **Champion pool + stats**, **lane execution**, **rank, roles, mastery** (recent games as a list were not chosen) |
| 3 | Profile shape | A **brief version with summaries on the Roster page**, and **detailed stats on an expanded page** |
| 4 | Automatic pool | **Games, then mastery**: ranked games of the last 60 days in the role set the tiers; mastery fills in when there are few games; the user reviews the proposal before saving and can add or remove champions afterwards |
| 5 | Removing | **Bench is one button, delete is another** (delete asks for confirmation: it can't be undone) |
| 6 | What delete removes | The player, their pools and their **fetched** games (solo, flex). **Team history stays**: drafts in History, the games logged from them (`player_games` with `source = "logged"`) and the execution rows keep the old name |
| 7 | Checking the Add-player flow | Tests use a fake Riot client; in Stage B one real lookup and fetch is allowed, with a Riot ID the user gives, on a scratch database, nothing kept |

## What exists to build on

| Need | Already there |
| --- | --- |
| Players, active / bench, swap, forget | `data/pools.py` (`set_players`, `swap_player`, `bench`, `forget_player`), `POST /api/players/swap`, `DELETE /api/players/bench` |
| Linking a Riot ID to an API PUUID | `learning/history._re_resolve` (account-v1) |
| A player's ranked games, last 60 days | `learning/history.fetch_history` / `_fetch_player` → `player_games` (champion, role, win, queue, start). No KDA or CS yet |
| Tiers from games | `learning/comfort.py` (`scored_tier`: main ≥ 15 games and adjusted WR ≥ 50%, comfortable ≥ 5) |
| Mastery and rank for a Riot ID | `data/scout.py` (`scout()` fetches all mastery and league-v4 entries; `rank_label`, `mastery_age` for recency-weighted mastery) |
| Lane execution per game | `game_execution_roles` (draft, role, share, label, stats) and `learning/trends.py` (bands, biggest lever) |
| Execution hint per pool champion | `learning/execution_comfort.py` |
| Pool health per role | `engine/blind.pool_health` |
| Role shares per champion (to judge if a mastery champion is played in the role) | the role tables the scouting role guess uses (`data/scout.role_evidence`) |
| Rate limits while a crawl runs | the yield file the scouting uses (`cache/jobs/yield.json`) |

## The experience

### The Roster page (`#/pools`, renamed in the nav as today: Roster)

Console style (`docs/ui-redesign.md`). From top to bottom:

1. **Page header**: "Roster", one-line description, and the page's one primary action: **Add player**.
2. **The lineup**: five rows, one per role, in role order. Each row is the player's **profile summary**:
   - role (mono), Riot ID, rank (solo / flex, whichever is higher, with LP), "since" (days active in this role);
   - **pool strip**: up to 5 champion icons in tier order, each with its tier as a 2-letter mono tag (MN / CF / LN),
     "+3" for the rest;
   - **figures** in mono: games (60 days, in this role), win rate, KDA, and the lane execution average as a signed
     share with its label (e.g. `+0.12 · Wins lane`), with a 10-game trend arrow;
   - **pool health** (from `blind.pool_health`): one quiet line, e.g. "No blind pick" with ⚠ when it's a gap;
   - **pending**: "2 tier changes suggested" when the comfort re-scoring has proposals (opens the profile's pool);
   - **actions** on the right: **Profile**, **Swap** (opens the swap sheet for that role), **Bench**, and **Delete**
     (in a `⋯` menu so it's never hit by accident). Bench and Delete are separate buttons, as asked.
   - An empty role shows "No player" and **Add player** for that role.
3. **The bench**: a table of benched players: Riot ID, the role(s) they have pools in, rank, games (60 days), last
   active, and actions **Swap in** (choose the role; their pool for that role comes with them), **Profile**,
   **Delete**.
4. **Stats bracket** per active player stays editable, as a select in the profile header (not the lineup row: it's a
   setting, not a summary).

Pool import from text moves into **Add player** (step 3, "paste a pool") and the profile's pool menu ("Import from
text"); the big textarea leaves the page.

### Swap sheet (from a lineup row's Swap, or a benched player's Swap in)

A side sheet: the role, the player going out (to the bench) and a list to choose who comes in: benched players with a
pool in that role first (with their summary figures), then other benched players, then "Someone new…" (opens Add
player with the role set). One confirm button. Undo: a toast "Swapped X in for Y · Undo" for 10 seconds (it swaps
back).

### Add player (a stepped sheet, 4 steps, each with Back)

1. **Who.** Riot ID (`Name#TAG`) and role. On blur the app looks the account up (account-v1; one request) and shows
   the account found, its level and rank, or "No such account" (fix the ID) or "No Riot key: you can still add them
   and build the pool by hand" (skips to step 3 with an empty proposal). A Riot ID already on the roster says so and
   offers its profile instead.
2. **Reading their games.** One background fetch: their ranked games of the last 60 days (Solo/Duo, Flex, Ranked 5s),
   all mastery, and rank. A progress line ("38 of 74 games"), and Skip (go on with what's in). It pauses a running
   crawl the way scouting does. Typical: under a minute for 60 games on an app key, longer while a crawl yields.
3. **Review the pool.** The proposal (see "Automatic pool" below) as a table: champion, proposed tier (editable
   select), games and win rate in this role, mastery points, and where it came from (`games` / `mastery`), each with
   an include checkbox. Under it: "Also plays" (other roles with ≥ 15% of their games, read-only, so you see if they're
   really a flex player), a champion search to **add** anyone else with a tier, and "Paste a pool" (the old import
   format, for this player only).
4. **Where they go.** Active in the role (the current player goes to the bench; their name is shown) or onto the
   bench. Confirm saves the player and the pool in one request, mirrors `roster.json`, and starts the learning job
   for them (as swaps do today).

### The profile (`#/player/<riot-id>`, a page of its own)

Header: Riot ID, role, active / benched, rank per queue (solo, flex) with LP and the season's games, the stats bracket
select, and the actions (Swap, Bench, Delete; or Swap in, Delete on the bench). A window picker for the figures:
**last 20 games / 60 days**.

Sections, top to bottom:

1. **Summary tiles**: games, win rate, KDA, CS / min, vision / min, lane execution average (signed, with its label),
   comfort-weighted pool size. Tiles are `Stat`s: value in mono, label under it.
2. **Champion pool** (the main section): a `Table` per role they have a pool in (the active role first):
   champion, tier (editable select), lock, the suggested tier if the re-scoring differs (with **Accept**), games, win
   rate, KDA, CS / min, last played, mastery points, the execution hint (`execution_comfort`, when that Setting is on),
   blind safety. Sorted by tier then games. Row actions: remove from pool. Above it: "Add champion" and "Import from
   text". Champions they played in this role but aren't in the pool appear greyed under a "Not in pool" divider with
   **Add**, so the pool can catch up with what they actually play.
3. **Roles**: their games by role (a single stacked bar with the share written beside each role), and per role games
   and win rate.
4. **Lane execution**: from the team games they played in this role (`game_execution_roles` for drafts where they were
   the active player in it): average share with a band (`trends.band`), label counts (Wins lane / Even / Loses lane …),
   the biggest lever (`trends.biggest_lever`), and a line of the last 20 games' shares. "Needs 5 team games" when there
   are fewer.
5. **Mastery**: the top 10 champions by mastery, with points and last played, and which of them are in the pool.

## Automatic pool (decision 4)

`learning/pool_proposal.py`, pure and tested. Input: the player's games (champion, role, win, start) and mastery
(champion, points, last played), the role, and the champion role shares. Output: a list of `{champ_id, role, tier,
source, games, wins, mastery}`.

- **From games** (the role's games in the last 60 days, all three ranked queues):
  - main: ≥ 15 games, or ≥ 25% of their games in the role with ≥ 8 games, and an adjusted win rate ≥ 45%
    (`comfort.wr_adj`);
  - comfortable: ≥ 5 games;
  - learning: 2–4 games, or 1 game in the last 14 days.
- **Then mastery**, only while the games give fewer than 5 champions in the role: champions with ≥ 50,000 points,
  played in the last 180 days (`mastery_age`), and a role share ≥ 10% for that role in the crawl. 200,000+ points →
  comfortable, else learning. Never main from mastery alone.
- At most 10 champions per role, games first, then mastery by points.
- These thresholds are constants at the top of the module so they can be tuned; the review step shows each one's
  source, so a wrong guess is visible and one click to fix.

## API

All under `api/pool_routes.py` (or a new `api/roster_routes.py`). Players are addressed by Riot ID (URL-encoded), as
`DELETE /api/players/bench` already is; the internal key changes when a player links, the Riot ID doesn't.

| Method and path | What |
| --- | --- |
| `GET /api/roster` | the lineup and the bench, each player with their summary (the figures in "The Roster page") |
| `POST /api/players/lookup {riot_id}` | account-v1: `{found, riot_id (canonical case), level, rank}` or `{found: false}` / `{no_key: true}` |
| `POST /api/players/onboard {riot_id, role}` | starts the fetch; returns `{job}` |
| `GET /api/players/onboard/{job}` | `{status, games_read, games_total, proposal, also_plays}` |
| `POST /api/players {riot_id, role, active, pool: [{champ_id, role, tier}]}` | creates the player and their pool in one transaction |
| `POST /api/players/{riot_id}/bench` | benches an active player |
| `POST /api/players/{riot_id}/activate {role}` | swaps them in for the role (the current player goes to the bench); returns what to undo |
| `DELETE /api/players/{riot_id}?confirm={riot_id}` | deletes the player, their pools and their fetched games, not the team's logged games (decision 6; active or benched; `confirm` must repeat the Riot ID) |
| `GET /api/players/{riot_id}/profile?window=20\|60d` | the detailed profile (every section above) |

The old `PUT /api/players`, `POST /api/players/swap` and `DELETE /api/players/bench` keep working until the old page
is gone, then go (with their tests moved to the new routes). Like a pool edit today, roster changes reach Live's
suggestions after the current draft.

## Data

- `player_games` gets `kills`, `deaths`, `assists`, `cs` (lane + jungle minions), `vision`, `duration_s`. Filled by
  `_fetch_player` from the match it already reads, and for games stored before, from the Riot cache (`riot.match` is
  cached, so no API calls): a one-off backfill run by the learning job. Logged games without a match stay without them;
  figures say how many games they're from.
- `players` gets `rank_json` (league-v4 entries), `mastery_json` (`[[champ_id, points, last_played_ms], …]`) and
  `profile_fetched_at`. Filled by the onboarding fetch and refreshed by the learning job at most once a day. Columns
  are added by `db/session`'s automatic `ADD COLUMN`.
- `roster.json` keeps what it has (no PUUIDs); rank and mastery aren't mirrored (they're fetched again).
- Lane execution per player: a team game belongs to a player when `player_games` has it under their key with
  `source = "logged"` (or the match-v5 row for the same game) in that role. That's how logged games are already filed
  (`docs/player-roster.md`, decision 5).
- Demo mode: seed synthetic games, mastery and rank for the demo roster (clearly marked like the rest of the demo
  data), so the pages can be checked without a key.

## Stages

Each stage: tests with it, `uv run pytest`, screenshots in demo mode at 1440 × 900 and 1100 × 760, then stop.

| Stage | What | Acceptance |
| --- | --- | --- |
| A | Data: the new columns, their filling and the backfill from the cache; `learning/profile.py` (pure: summary and profile figures from games, pool entries, execution rows, mastery, rank) | unit tests on planted games; a backfilled database gives the same figures as freshly fetched games |
| B | `learning/pool_proposal.py` and the onboarding API (lookup, start, status, create) with a fake Riot client | proposal tests for each rule (games only, mastery fill, caps, never main from mastery); the whole flow against the fake client; no key → empty proposal |
| C | Roster actions (bench, activate with undo, delete with confirm) and `GET /api/roster` | tests: bench keeps the pool, activate brings the role's pool back, delete removes pool and fetched games and keeps logged team games, a wrong `confirm` is refused, `roster.json` follows every change |
| D | The Roster page: lineup summaries, bench, swap sheet, Add player sheet | screenshots of each step of Add player, the swap with its undo toast, an empty role; keyboard-only run through Add player |
| E | The profile page | screenshots of an active and a benched profile, a player with no team games, a player with no key data |

## Not in scope

- A list of recent games on the profile (not chosen; History has the team's games).
- Automatic swapping when a benched player shows up in champ select (`docs/player-roster.md`, decision 6).
- Profiles of enemy players (scouting has its own view on Live).
