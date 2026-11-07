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

## Decisions

1. **Up to 20 games per player, every ranked queue, the last 60 days (Stage A).** With the key's 100 requests per 2
   minutes, a new team costs about 110 requests (2 each for account and mastery, 1 for match ids, 20 matches), so about
   2 minutes; mastery for all five is in within the first second or two. Solo/Duo and Flex count as well as Ranked 5s:
   a player's Ranked 5s games alone are too few. A player scouted in the last 6 hours isn't fetched again.
2. **Riot IDs match whatever the case (Stage A),** as Riot treats them. The first spelling seen is kept.
3. **One background batch at a time (Stage A).** Scouting runs in the app's process with its own rate limiter. A crawl
   running at the same time in another process shares the key's limits, so both may slow down and wait on 429s;
   `RiotClient` already waits them out.
