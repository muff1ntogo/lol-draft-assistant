# Task: player roster: subs, the bench, and a local copy of the pools

> Since 13 Nov 2026 the page described here is the Roster page of `docs/roster-profiles.md`: "Swap in a sub" is its
> swap sheet, Forget is Delete (for any player, with a confirmation), and the players table is gone (players are added
> with Add player). The data model, the bench and the roster file below are unchanged. The old routes (`PUT
> /api/players`, `POST /api/players/swap`, `DELETE /api/players/bench`) still exist; no page calls them.

Built 7 Nov 2026, at the user's request: "player pool caching on local files (not affected by updates) + player
swapping (caching past player pools but able to swap to new players for subs)".

## What changed

- **One active player per role, and a bench.** `players.active` (new column, true for everyone already there). The
  engine, Live, the Pools page and the learning job read only the active players; a benched player keeps their pool,
  tiers, locks and stored games.
- **Swapping.** Pools page, "Swap in a sub": a role and a Riot ID. Someone new comes in with an empty pool (and the
  role's stats bracket); someone on the bench comes back with theirs. The player they replace goes to the bench.
  `POST /api/players/swap {role, riot_id}`; like a pool edit, it reaches suggestions after the current draft, and it
  starts the learning job for the new player's games.
- **Changing a Riot ID in the Players table** swaps too, when it's someone else: a benched player's Riot ID brings them
  back; a linked player (someone real) goes to the bench. A player never linked is renamed instead, pool and all, so a
  typo fixed doesn't lose the pool. Before, any new Riot ID took over the old player's pool.
- **Forget** (bench only): `DELETE /api/players/bench?riot_id=` deletes the player, their pool and their stored games.
- **The roster file.** `data/roster.json` in the data folder (`%LOCALAPPDATA%\DraftAssistant` when installed): every
  player, active and benched, with their pools (champion, role, tier, lock). Written after every change to players or
  pools (`Services.save_roster`), atomically, with the previous version as `roster.json.bak`. Updates never touch the
  data folder. At start, a database with no Riot IDs and no pools (a new install without `import-data`, a reset
  database) is filled from the file (or the `.bak` if the file is unreadable), then re-scored. `import-data` copies it.

## Decisions

1. **The database stays the working copy; the file is a mirror.** Everything already reads the database, and the file
   only restores into an empty roster, so it can never undo an edit.
2. **No PUUIDs in the file.** Players link again from their Riot IDs (account-v1 with a key, or the next champ select).
   Riot IDs are real names, so the file is in `.gitignore` like the database.
3. **A player's pool entries keep their roles.** Benched from top and brought back as mid, a player starts mid empty;
   their top pool waits for them under top.
4. **Placeholders are per player:** `unresolved:<role>`, then `unresolved:<role>:2`, ..., so two unlinked players can
   share a role (one on the bench).
5. **Logged games go to whoever is active in that role** when the learning job runs, which is right after each game.
6. **Not built:** swapping automatically when a benched player shows up in champ select. Live only knows the active
   players, so a benched player there looks like someone unknown until swapped in.
