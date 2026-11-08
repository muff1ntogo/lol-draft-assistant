# Scouting: how reliable can it be? (research, 8 Nov 2026)

Asked after `ui-engine-review.md` (answer 7): Live is fast enough; the concern is how well scouting works and how to
get it close to a guaranteed success rate. Nothing here is built yet. A proposed plan is at the end.

## 1. The bottleneck is knowing who they are, not fetching their data

Scouting has two steps: **identify** each enemy player (a Riot ID or PUUID), then **fetch** what they play. The fetch is
under our control and can be made near-certain. Identification is not: it depends on what the client shows in champ
select, and there is no Riot API that names an anonymous enemy during champ select.

What the client gives in `theirTeam`, per queue:

| Queue | Enemy identity in champ select | Source |
| --- | --- | --- |
| Ranked Flex (440) | **None.** No name, tag or PUUID field at all, only cell, position, champion, spells | our recording `fixtures/lcu/flex-ranked-sample` |
| Ranked Solo/Duo (420) | **Hidden by Riot** since patch 12.22 (anonymised; the data is withheld in queue 420 lobbies) | Riot dev post, below |
| Ranked 5s (our main queue) | **Unknown** until the first recorded Ranked 5s champ select | open point in CLAUDE.md |
| Clash (700) | Known before champ select: the opposing team's roster is public in the bracket | clash-v1 |

**Policy.** Riot asked apps that reveal player names or stats in champ select to remove that feature (patch 12.22;
Solo/Duo, and per an edit Flex too); Overwolf's compliance rules for League say non-party names in Ranked Solo/Duo champ
select must stay hidden, and that names are visible from the loading screen on. So de-anonymising enemies in champ
select (reading hidden client data, guessing accounts from champions and spells) is off the table: it breaks Riot's
developer policies and would put the approved app key at risk. Where the client shows names (Ranked 5s if it does,
Clash, customs), or where you paste names you can legitimately see, scouting is fine, as today.

**Consequence:** "close to guaranteed" is achievable only in queues that show enemy names. In Flex and Solo/Duo the
honest best is: meta-only during the draft, full scouting from the loading screen on (section 3).

## 2. Making the fetch near-certain when we have names

Checked against the live API through this environment's proxy (8 Nov 2026): `league/v4/entries/by-puuid`,
`spectator/v5/active-games/by-summoner/{puuid}`, `champion-mastery/v4/champion-masteries/by-puuid` (all champions) and
`clash/v1/players/by-puuid` all exist on EUW (they answer 400 to a bad PUUID, not 404).

Today's cost per team: per player 1 account + 1 mastery + 1 match list + up to 20 matches = 23 requests, so **115 for
five players**, over the key's 100 per 2 minutes: the last games arrive 2–3 minutes in, after the bans.

| Improvement | Effect |
| --- | --- |
| **Read each match once for all five.** A premade (Ranked 5s, Clash) plays most games together: one match JSON holds all of them. Collect the five match lists first, read the union, and file each game under every scouted player in it | the whole team in ~25–45 requests instead of 115: about **20–40 s** instead of 2–3 min |
| **Mastery: all champions, not the top 20** (`champion-masteries/by-puuid`, same one request) | every champion's points and last-played time: a better recency prior for one-tricks of a less-played champion |
| **Rank per player** (`league/v4/entries/by-puuid`, 1 request each) | each enemy scored in their own bracket instead of the Gold prior; "Diamond jungler" shown on Live |
| **Order by what the bans need**: mastery for all five, then the first 5 games of each, then the rest | something useful for every player by ban 1 even on a slow key |
| **Retry on transient errors** (5xx, timeouts) with backoff inside the fetch, not once per game | fewer "error" players |
| **Pre-scout Clash** from the bracket (`clash/v1`) when the next opponent is known | everything cached before champ select starts |
| **Record and measure**: per game, where the IDs came from, how many resolved, time to mastery and to 5 / 20 games, failures by kind; a "Scouting" row in History and a summary in `scout-check` | we see the real success rate instead of guessing |

## 3. Every queue: scouting from the loading screen

`spectator/v5/active-games/by-summoner/{our player's PUUID}` returns, once the game has started (loading screen
onwards), all ten players with **PUUID, Riot ID, champion, both summoner spells and the full rune page**: checked on a
live Solo/Duo game (queue 420). Names are visible to players from the loading screen, so this is within the rules for
every queue, Flex and Solo/Duo included. One request.

What it allows:
- **Strategy tab, in the loading screen:** each enemy's rank, games and win rate on the champion they locked, their
  main role, and whether they're off-role (spells: Smite = jungle) or autofilled; "Track these" names the player and
  how experienced they are on it. (Allowed: the rules forbid in-game cooldown and timer tracking and game-state
  directions, not pre-game player information. The Strategy tab would show nothing new once the game is underway.)
- **Their real runes and spells** for the lane plan (e.g. Ignite vs Teleport, Conqueror vs Grasp).
- **Remember who we played:** store the ten PUUIDs with the draft (History "who we played", and cached pools for a
  premade met again).

It cannot help the draft itself: it only answers once the game exists.

## 4. Proposed plan (for your go-ahead)

1. **Record one Ranked 5s champ select** (recording is on by default, `DRAFTASSIST_RECORD`), then run `draftassist
   scout-check`: it tells us whether Ranked 5s shows enemy names. This decides how much of section 2 matters.
2. **Fetch improvements** (section 2): one read per match for the team, full mastery, rank per player, ban-first order,
   retries, and the success measurements.
3. **Loading-screen scouting** (section 3) into the Strategy tab, for every queue.
4. **Clash pre-scouting**, only if you play Clash.

## Sources

- [Riot dev post: apps that reveal player names or stats in champ select need to remove them (patch 12.22)](https://devtrackers.gg/leagueoflegends/p/749a443a-apps-that-reveal-player-names-or-stats-in-champ-select-need-to-remove-these-informations-starting-patch-12-22)
- [Overwolf: League of Legends game compliance](https://dev.overwolf.com/ow-electron/guides/game-compliance/league-of-legends/)
- [Overwolf: League launcher game events (champ select session fields)](https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/league-of-legends-launcher)
- [Riot Developer Portal: API list](https://developer.riotgames.com/apis)
