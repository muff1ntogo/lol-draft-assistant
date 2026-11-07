# Task: Enemy scouting: bans and enemy picks from the players we face

The spec's Phase 6 scouting, for Ranked 5s, where the user can see the enemy team's names in champ select. Today bans and
predicted enemy picks come from the enemy bracket's meta alone (`engine.py`: Threat, Matchup and the "N% likely" text all
read the chance that their player in a role is on X, from pick rates). With scouting, that chance blends in what each
enemy player actually plays.

Built in stages, tests first, each stopped for review.

## Stage A: scouting data

- **Who:** the enemy's Riot IDs. From the client when it gives them (`theirTeam[].gameName` / `tagLine`, already read by
  `lcu/parser.py`), or pasted by hand. `draftassist scout-check` reports, from the recordings in `fixtures/lcu/recorded/`,
  which identity fields each queue's `theirTeam` carries (field names and counts only, never values).
- **What, fast:** account-v1 (Riot ID to API PUUID) and champion-mastery-v4's top 20: two requests per player, so the
  whole team in about a second, before the bans.
- **What, then:** their ranked games of the last 60 days from match-v5 (up to 20 per player, every ranked queue), fetched
  in the background across the five players in turn, so all five fill in evenly. Every response is cached, so a team met
  again loads at once.
- **Stored:** `scout_players` (Riot ID, API PUUID, mastery, when fetched, status) and `scout_games` (one row per player and
  match: champion, role, win, queue, start). Local only, like the rest of the database.
- **API:** `POST /api/scout` with Riot IDs starts a fetch; `GET /api/scout` gives each player's status and pool.

## Stage B: the engine

- **A player's pool per role:** their share of games on each champion in that role; with no games yet, their mastery
  points spread over the champions the meta plays in that role.
- **The blend (spec):** the chance their player in role r is on X = (1 - s) x meta + s x player, with s = 0.6 x n / (n + 5)
  for n games in that role (mastery alone counts as n = 5). Settings: the 0.6.
- **Their roles:** from `assignedPosition` when the client gives it, else each player's most played role.
- **Where it counts:** the lane ban tables' opponent weights (so Threat and Matchup), the enemy pick model (the "N% likely"
  text and likely enemy picks), and ban reasons, which name the player ("their top: Irelia, 34 games, 61%").

## Stage C: the screens

- Live: each enemy slot shows the player's top champions once scouted (and "scouting..." until then); ban cards say
  when a reason comes from scouting.
- A "Scout opponents" box (Live and Prep) to paste Riot IDs or an OP.GG multi-search when the client doesn't give them.
- Settings: the scouting weight, and scouting on or off.

## Stage E: accurate with less to go on (7 Nov 2026)

Scouting is usually incomplete when it matters: the bans start before the match history is in, some players have few
games in their role, mastery is lifetime, and Flex gives no positions. Stage E makes the partial picture count for more
and mislead less. Decisions 13–17.

## Decisions

1. **Up to 20 games per player, every ranked queue, the last 60 days (Stage A).** With the key's 100 requests per 2
   minutes, a new team costs about 110 requests (2 each for account and mastery, 1 for match ids, 20 matches), so about
   2 minutes; mastery for all five is in within the first second or two. Solo/Duo and Flex count as well as Ranked 5s:
   a player's Ranked 5s games alone are too few. A player scouted in the last 6 hours isn't fetched again.
2. **Riot IDs match whatever the case (Stage A),** as Riot treats them. The first spelling seen is kept.
3. **One background batch at a time (Stage A).** Scouting runs in the app's process with its own rate limiter. A crawl
   running at the same time in another process shares the key's limits, so both may slow down and wait on 429s;
   `RiotClient` already waits them out.
4. **Ban cards split by their pools (user, 7 Nov 2026).** Bans 1–3: the 5 best bans from the enemy players' pools, then
   the 2 best from outside them. Bans 4–5: 3 from their pools, then 1 from outside. Short on pool bans, the outside list
   fills the gap, so the count stays the same. Both lists still need a positive ban value (ban-guardrails.md Rule 3).
   Flex's simultaneous bans are unchanged (no scouting there).
5. **"In their pool" (Stage B):** 2+ recent games on it in any role, or one of their top 3 mastery champions, for a
   player whose role the enemy hasn't filled yet. When two players have it, the one with more games owns it.
6. **The blend's confidence (Stage B):** s = 0.6 x n / (n + 5) for n games in the role, so 5 games give 0.3, 20 give
   0.48; mastery alone counts as 5 games, spread over the champions the meta plays at least 10% in that role.
7. **Their roles without the client's positions (Stage B):** the player with the most games picks their most played role
   first, and so on; a player with no games and no position isn't placed.
8. **An off-meta pick in their pool (Stage B)** has no stats in that role: its Threat uses its most played role's win
   rate, else 50%. Found when the first scouted run crashed on it.
10. **Live scouts by itself (Stage C).** When the client gives the enemy's Riot IDs, each player is asked for once per
    game (so a refresh never sets off another fetch); Live updates as their pools fill in. Pasted IDs are used only
    when the client gives none, and are forgotten when the game ends. On by default; Settings has the switch and the
    weight (0 to 1).
11. **Never in demo mode (Stage C).** The demo's enemy names are made up and could belong to real players.
12. **Follow-up: the enemy's roles ignore the client's positions.** The engine assigns their picks to roles by how
    likely each champion is there, so an off-role pick (a toplaner on Ezreal) can mark the wrong role as filled, and
    scouting then drops the wrong player's pool. Ranked 5s gives `assignedPosition` for the enemy: using it in the
    engine's role assignment would fix this. Seen in the Stage C replay check. **Done in Stage D (user, 7 Nov 2026):**
    `DraftState.their_positions` carries the client's positions for their locked picks; the engine fixes those roles
    (certain in `role_filled_probs`) and guesses only the rest. Without positions (Flex) nothing changes.
9. **Ban reasons name the player (Stage B):** "Their top laner (Name): 20 games, 70% · Our top's best answer is -2.8pp",
   or "mastery #1" without games. The rest of the line is the reason it had before.
13. **Mastery is always a prior, weighted by recency (Stage E).** A player's share in a role is their games there, plus
    their games on the same champion in other roles at 0.25 (when the meta plays it in this role), plus their mastery
    worth 5 games, spread by sqrt(points) × 0.5^(days since they last played it / 30). Before, one game in the role
    replaced the mastery entirely (100% on that one champion), and a main abandoned a year ago counted as much as
    today's. sqrt tames the one-trick totals of 2M points. `lastPlayTime` is now stored with the mastery
    (`[champ, points, last ms]`; older rows still load, undated). A top-3 mastery champion not played for 90 days is no
    longer "in their pool" (ban split, reasons), and Live's slot shows recently played mastery first.
14. **Their roles are guessed jointly, mastery included (Stage E).** Without the client's positions, the free roles go
    to the most likely joint assignment (sum of log of each player's share of evidence in the role: games, plus mastery
    worth 3 games spread by the meta's role shares), not greedily by who has most games. So a player with only
    mastery so far gets a role, and a top/mid player doesn't push out a top-only one. A player with no evidence for any
    free role stays unplaced.
15. **How sure we are of the role scales the blend (Stage E).** s is multiplied by the chance they play that role (the
    softmax marginal over every assignment); 1 when the client gives positions. A wrong guess costs less.
16. **A banned main's share goes to the rest of their pool (Stage E).** The lane ban tables (Threat, Matchup) now
    renormalise the player's share over the champions still available, as the enemy pick model already did.
17. **Partial data still flows (Stage E).** A player whose fetch failed (not a bad key) is asked for once more in the
    game, 30 s after. Pasted Riot IDs fill in for players the client doesn't name (up to five; no position). A game
    without a `teamPosition` uses `individualPosition` for its role.
