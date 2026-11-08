# Task: import games History missed

Built 8 Nov 2026, at the user's request ("there's some games that have taken place but are not showing up in history").

## Why games go missing

History logs a draft while the app watches champ select (`api/draftlog.py`) and attaches the result at the end of the
game. A game is never logged if the app wasn't open (or connected to the client) during its champ select, or closed
between champ select and the game starting. `cache/launcher.log` says which: "logged draft", "champ select ended
without a game" (a dodge), "no logged draft to attach the result to" (a game whose champ select it missed).

## What it does

**Import missed games** (History page, top of the list; also the Crawl page's job `import`, and
`draftassist import-games`): `data/import_games.py`.

1. Our active players' API PUUIDs (looked up from their Riot IDs where needed).
2. Each one's ranked match ids of the last 60 days (cached by `RiotClient`).
3. Matches in the histories of at least 3 of them (`MIN_TEAMMATES`): games we played together. Only these are
   fetched.
4. Each one not in History yet (by game id; or a draft logged live without a game id within 20 minutes of the start,
   which gets the game id) becomes a draft: side, result, queue, length, patch, both teams' picks, bans by pick turn,
   our picks' roles from `teamPosition`, our players' names. `drafts.imported` marks it; it counts as verified
   (`result_source` match-v5).
5. Its timeline (one call per game), then the per-game split for the new games.

## Decisions

1. **Pick order is approximate.** match-v5 has no pick order; picks fill the format's pick steps in the participants'
   order (the champ select order). The History page says so on the "imported" label.
2. **No suggestions, no predicted P(win).** The app didn't see the champ select: imported games have no suggestion
   ranks (left out of "followed our #1") and no `pred_win_prob` (left out of the Model check). The win-probability line
   per step fills in on first view, as for any old game.
3. **Format by queue:** Solo/Duo (420) and Flex (440) are standard draft; any other ranked queue is Ranked 5s
   (tournament draft).
4. **Remakes (under 5 minutes) aren't imported.**
5. **Benched players don't count** towards the 3: the roster now is who "we" are.
6. **Runs as a job** (its own process and rate limiter, like the other Riot jobs), so it waits for a running crawl.
