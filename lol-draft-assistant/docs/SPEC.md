# Draft Assistant for Ranked 5s: Project Spec

Nov 1, 2026 · @Andrew

## Overview

A local Windows app that reads Ranked 5s champ select live from the League client. It gives our shot-caller the top 3 bans or picks at every turn, each with a one-line reason, then learns from our results after every game.

**Team (EUW).** A mixed-elo 5-stack, so stats are pulled per player at their own bracket.

| Role | Rank | Stats bracket |
| --- | --- | --- |
| Top | Gold | Gold |
| Jungle | Emerald | Emerald |
| Mid | Gold | Gold |
| ADC | Gold | Gold |
| Support | Iron | Iron |

**Success criteria**

- Suggestions refresh within 1 second of any champ select change, with zero manual entry during the draft.
- The shot-caller can read a suggestion and call it in voice in under 5 seconds.
- After every logged game, each player's pool is re-scored as main / comfortable / learning.
- After 30 logged games, the model's predicted win probabilities score better than a 50% coin-flip baseline (Brier score).

**Using this doc with Claude Code.** Export it to Markdown and save it as `docs/SPEC.md` in a new repo. Add a short `CLAUDE.md` that points to the spec and says: build one phase at a time from the Build plan, run tests before moving on, and ask before changing scope. Put the prototype `draft_engine.py` in `reference/`.

## Ranked 5s context and constraints

Ranked 5s is a time-limited experiment, so the app must not depend on it existing. Facts as of 1 Nov 2026 ([Riot support](https://support.riotgames.com/en-us/league-of-legends/events/ranked-5s)):

| Item | Detail |
| --- | --- |
| Current run | Experiment 2: 11 Sep 2026 to 7 Jan 2027 |
| Schedule | Fri to Sun, 4pm to 4am server time |
| Draft | Tournament draft: 3 bans, 3 picks, 2 bans, 2 picks per team |
| Party | Full team of five only; no rank restrictions |
| Scouting | A scouting tooltip on opponents during pick/ban (contents not documented) |

In standard ranked, opponents' names and roles are hidden until the game begins ([League wiki](https://wiki.leagueoflegends.com/en-us/Blind_pick)). Riot hasn't documented whether Ranked 5s does the same.

**Design consequences**

- **Enemy unknown by default.** Predict enemy picks from their elo bracket's meta plus the picks they've already revealed. Switch to scouting only if the client exposes enemy players.
- **Draft format is config.** Support tournament draft and standard ranked draft, so the app still works in Flex or Clash if Ranked 5s ends on 7 Jan.
- **Mixed elo.** Meta stats come from each player's own bracket. The enemy prior uses the team's average bracket (Gold).
- **Two ban phases.** Phase 1 bans deny meta threats. Phase 2 bans protect our picks and target the enemy's open roles.

## Scope

The MVP is a working live assistant on rule-based scoring. Learning and scouting come in v2.

**MVP**

- Read-only live champ select from the League client (LCU).
- Champion pools imported from text, with a manual tier per champion.
- Meta stats crawled from the Riot API (EUW; Iron, Gold, Emerald brackets), bootstrapped from OP.GG's MCP server.
- Draft engine v1: additive log-odds scoring, ban and pick suggestions, a meta-based enemy model.
- Shot-caller web UI: top 3 suggestions + one-line reason.
- Auto-logging of every draft and its result.

**v2**

- Comfort tiers re-scored from results after each game.
- Post-game draft report: predicted vs. actual.
- Team style suggestion from recent results (advisory only; the style itself is set by hand).
- Enemy scouting when the client exposes opponents.
- A trained win-probability model and a 2-turn lookahead.

**Non-goals**

- Auto-hovering, picking, locking, or any other write to the client.
- Runes, items, in-game overlays.
- Public hosting or multi-team support.
- Solo/Duo queue.

## Core workflows

There are four workflows: one-time setup, pre-session prep, the live draft, and post-game learning.

1. **Setup (once).** Enter the five Riot IDs and their roles. Paste each player's pool as text (format under Data model). Run the first data refresh.
2. **Pre-session prep (before Friday's queue).** Refresh stats for the current patch. Generate a prep sheet:
   - Priority bans for blue side and for red side.
   - Each player's best 3 champions this patch, blending comfort and meta.
   - Safe blind first picks and flex picks from our pools.
3. **Live draft.**
   - The app detects champ select and shows our side, the phase and whose turn it is.
   - On our turn: top 3 bans or picks, each with a one-line reason.
   - On the enemy's turn: their 3 most likely picks and our prepared answers to each.
   - Teammate hovers (pick intent) are scored, and a hover that's easy to counter gets flagged.
   - At lock-in: model win probability and the two biggest threats in the enemy comp.
4. **Post-game.**
   - When the game ends, fetch the match from match-v5 and log the draft and result.
   - Re-score comfort tiers (v2) and generate a draft report (v2).
   - Weekly: suggest a team style from recent results (v2).

## Architecture

Six components run in one Python process on the Windows PC that runs League. The draft engine is pure functions with no I/O, so it can be tested in isolation.

| Component | Responsibility | Talks to |
| --- | --- | --- |
| LCU connector | Subscribes to champ select and gameflow events; converts them to a `DraftState` | League client (localhost), engine |
| Draft engine | Scores states; returns ranked bans/picks with reasons | Stats store (read-only) |
| Data service | Crawls the Riot API, Data Dragon and OP.GG; aggregates stats per patch and bracket | SQLite |
| Learning jobs | Comfort re-scoring, model training, draft reports, style suggestion | SQLite |
| API server | FastAPI REST + WebSocket push of suggestions | Engine, SQLite, UI |
| Web UI | Shot-caller screen on `localhost:8000` (second monitor) | API server |

**Live flow:** League client → LCU connector → engine → API server → UI (WebSocket push on every change).

**Post-game flow:** gameflow `EndOfGame` → fetch the match from match-v5 by game ID → log → learning jobs.

## Data sources and ingestion

The Riot API is the backbone. OP.GG only bootstraps us until our own crawl covers the patch.

| Source | What we take | Refresh |
| --- | --- | --- |
| Riot league-v4 (`euw1`) | Seed players per bracket (Iron, Gold, Emerald) | Weekly |
| Riot match-v5 (`europe`) | Matches for meta, matchup and synergy stats; our own games | Nightly in season |
| Riot account-v1 | Riot ID → PUUID for our five players (and scouted enemies) | On setup |
| Riot champion-mastery-v4 | Mastery, to sanity-check imported pools | Weekly |
| Data Dragon | Champion IDs, names, icons, patch version | Each patch |
| OP.GG MCP server (`https://mcp-api.op.gg/mcp`) | Win/pick/ban rates, positions, counters for the bootstrap | On demand, cached |
| LCU (local client) | Live champ select, gameflow phase, queue ID, game ID | Live |

**Crawler**

1. For each bracket, pull ranked entries from league-v4 and resolve PUUIDs.
2. Pull ranked match IDs per PUUID (Solo/Duo and Flex), current patch only, deduplicated.
3. Fetch each match. From `participants` store `championId`, `teamPosition`, `win`; from `teams.bans` store `championId`.
4. Aggregate into per-patch tables: role stats, lane matchups (same `teamPosition`), and same-team duo synergy.

**Rate limits.** At development-key rates (100 requests every 2 minutes) the crawl does about 3,000 matches an hour, so run it overnight and cache everything. Check your key's actual limits on the developer portal.

**Small samples.** Shrink every rate towards 50% before use: `wr_adj = (wins + 0.5·k) / (games + k)` with `k = 200` for matchups and synergy, `k = 50` for role win rates.

## Data model

One SQLite file holds everything. Every stats table is keyed by patch and bracket.

| Table | Key columns | Purpose |
| --- | --- | --- |
| `champions` | `champ_id`, `key`, `name`, `roles` | Static data from Data Dragon |
| `champ_role_stats` | `patch`, `bracket`, `champ_id`, `role` → `games`, `wins`, `picks`, `bans` | Meta strength, pick and ban rates |
| `matchups` | `patch`, `bracket`, `role`, `champ_a`, `champ_b` → `games`, `wins_a` | Lane counters |
| `synergy` | `patch`, `bracket`, `champ_a`, `champ_b` → `games`, `wins` | Same-team duos |
| `players` | `puuid`, `riot_id`, `role`, `rank`, `bracket` | Our five |
| `pool_entries` | `puuid`, `champ_id`, `role` → `manual_tier`, `scored_tier`, `tier_locked`, `comfort`, `games`, `wins`, `last_played` | Pools and comfort |
| `drafts` | `draft_id`, `game_id`, `queue_id`, `side`, `played_at`, `result`, `pred_win_prob`, `style_tag` | One row per game |
| `draft_actions` | `draft_id`, `seq`, `side`, `type`, `champ_id`, `our_rank`, `suggestions_json` | Every ban/pick, with what we suggested |
| `enemy_scout` (v2) | `puuid`, `champ_id`, `role` → `games`, `wins` | Scouted opponents |

**Pool import format.** Plain text, one block per player. The tier is the starting prior: `main`, `comfortable` or `learning`.

```
TOP
K'Sante: main
Rumble: comfortable
Gnar: learning

JUNGLE
Sejuani: main
```

**Comfort scoring (v2).** After each game, for every pool champion:

- `comfort = 0.6·(wr_adj − 0.5)·10 + 0.4·log(1 + recent_games)`, where `recent_games` counts the last 60 days across Ranked 5s, Flex and Solo/Duo.
- `wr_adj` shrinks towards the manual tier's prior: main 53%, comfortable 50%, learning 46%, with `k = 10` games.
- **Main** = 15+ recent games and `wr_adj` ≥ 50%. **Comfortable** = 5+ recent games. **Learning** = everything else.
- The UI shows tier changes for the player to accept. A locked tier is never overwritten.

## Draft engine

The engine scores any partial draft as the log-odds that we win, then ranks every legal action by how much it changes that score. Start from `reference/draft_engine.py`.

**Score (v1).** For our comp A and the enemy comp B, with roles assigned by brute force (at most 5! = 120 permutations):

```latex
L = \sum_{i \in A} \left( \alpha\, m_{i,r_i} + \beta\, c_i \right) - \sum_{j \in B} m_{j,r_j} + \sum_{\{i,k\} \subset A} s_{ik} - \sum_{\{j,l\} \subset B} s_{jl} + \sum_{r} x_{A_r, B_r}
```

- m = logit of the bracket win rate for that champion in that role (our players' brackets for A, Gold for B).
- c = the player's comfort score; s = duo synergy; x = lane matchup logit for the champions sharing role r.
- **Balanced weighting:** α = β = 1 by default, configurable. P(win) = sigmoid(L).
- Off-pool champions get a −0.4 comfort penalty, so they're possible but rarely suggested.

**Enemy model.** P(enemy picks c) = softmax over the bracket's pick rate and strength for roles they haven't filled yet. Infer their filled roles from revealed picks. When scouting is available, blend 60% player history with 40% meta.

**Ban value** = P(enemy picks c) × harm(c) + 0.15 × protect(c), where harm is the drop in L if they get c, and protect is how hard c counters our likely picks.

- Phase 1: harm weighted by meta, a "global" ban.
- Phase 2: restricted to the enemy's open roles, weighting counters to the champions we've already locked.

**Pick value** = new P(win) − 0.1 × counter risk + flex bonus.

- Counter risk = the best counter still open to the enemy, scaled by how many picks they have left after ours.
- Flex bonus = 0.02 per extra role the champion can play in our team's pools (it hides our role plan).

**Reasons.** Each suggestion shows the largest contributing term as a one-line template, e.g. "Mid main, counters their Azir" or "Their Gnar: 14% likely, hard counters our K'Sante".

**v2.** Replace the hand-set weights with logistic regression trained on crawled matches and our logged games. Add a 2-turn expectimax lookahead (beam width 8) on our picks. Budget: under 200 ms per suggestion set.

## Live champ select integration (LCU)

The app talks to the client's local API through `lcu-driver`, read-only: GET requests and event subscriptions only, never PATCH or POST.

| Endpoint / event | Used for |
| --- | --- |
| `/lol-gameflow/v1/gameflow-phase` | Detect `ChampSelect`, `InProgress`, `EndOfGame` |
| `/lol-gameflow/v1/session` | Queue ID, game ID, map |
| WebSocket `/lol-champ-select/v1/session` | Live draft: `actions`, `myTeam`, `theirTeam`, `bans`, `timer`, `localPlayerCellId` |
| `/lol-summoner/v1/current-summoner` | Confirm which of our five is running the app |

**Parsing rules**

- `actions` is a list of turns; each turn holds actions with `type` (ban/pick), `actorCellId`, `championId`, `completed`, `isAllyAction`. A completed action with `championId` 0 is a skipped ban.
- Our side comes from `myTeam[].team` (1 = blue, 2 = red).
- Hover = an incomplete pick action with a `championId` or a `championPickIntent` on `myTeam`. Treat it as provisional.
- Use `timer.phase` and the turn index to map the session onto the configured draft order.
- Keep all parsing in one module. Client patches change field names, so a fixture-based test should fail loudly.

**Day-one task.** In the first Ranked 5s game, dump every champ select session JSON to `fixtures/`. Check what `theirTeam` contains (PUUIDs, names, positions) and what powers the scouting tooltip. That decides whether v2 scouting is possible.

## UI

The Live screen is built for one glance on a second monitor: large type, dark theme, three suggestion cards, nothing to click during the draft.

**Live screen layout**

- **Top bar:** our side (blue/red), phase (Ban 1 / Pick 1 / Ban 2 / Pick 2), whose turn, countdown.
- **Left column:** our five slots (role, player, pick or hover) and our bans.
- **Right column:** the enemy's five slots (inferred role) and their bans.
- **Centre, our turn:** "BAN" or "PICK — \[role\]" plus 3 cards, each with icon, name and a one-line reason. The card for a teammate's current hover is highlighted.
- **Centre, enemy turn:** their 3 likely picks with probabilities, and our best answer to each.
- **After lock-in:** win probability and the two biggest enemy threats.

**Other pages**

| Page | Contents |
| --- | --- |
| Prep | Side-specific priority bans, each player's best 3 this patch, blind-safe and flex picks |
| Pools | Each player's champions with manual vs. scored tier; accept or lock changes; import text |
| History | Logged drafts with result, predicted win %, and the v2 draft report |
| Team | Recent form, the suggested style (v2), and the style set by hand |
| Settings | Riot API key, player Riot IDs, weights (α, β), draft format, data refresh |

## Tech stack and repo layout

Python end to end, with a thin web front end. It runs on the Windows side of the dual-boot PC, because the League client and its local API only exist there.

| Layer | Choice |
| --- | --- |
| Language | Python 3.12, managed with `uv` |
| Server | FastAPI + uvicorn; WebSocket for live push |
| Client API | `lcu-driver` |
| Riot API | `httpx` with a token-bucket rate limiter and on-disk response cache |
| Storage | SQLite via SQLModel |
| Analysis | `pandas`, `numpy`; `scikit-learn` in v2 |
| Scheduling | APScheduler (nightly crawl, post-game jobs) |
| Front end | Vite + Svelte (or React), served by FastAPI as static files |
| Tests | `pytest` |
| Secrets | `.env` holds `RIOT_API_KEY`; never committed |

```
lol-draft-assistant/
  CLAUDE.md
  docs/SPEC.md
  reference/draft_engine.py
  src/draftassist/
    lcu/          # connector + session parser
    engine/       # scoring, bans, picks, enemy model, draft formats
    data/         # riot client, crawler, aggregation, ddragon, opgg
    learning/     # comfort scoring, reports, style, model training
    api/          # FastAPI app, routes, websocket
    db/           # models, migrations
  web/            # front end
  fixtures/       # recorded LCU sessions, sample matches
  tests/
```

## Build plan for Claude Code

Seven phases, each ending in a passing acceptance check and a commit. Phases 0 to 4 are the MVP and can be used in a real weekend session.

1. **Phase 0: Skeleton.** Repo layout, `uv` project, `.env` loading, SQLite models, Data Dragon loader.
   - Accept: `pytest` passes; `champions` is filled for the current patch.
2. **Phase 1: Engine.** Port the prototype into `engine/`. Make draft formats configurable (tournament and standard ranked) and add reason strings.
   - Accept: a simulated 20-step draft completes in both formats. No banned or picked champion is ever suggested. Each suggestion set returns in under 200 ms.
3. **Phase 2: Live LCU + UI v0.** Connector, session parser, WebSocket push, Live screen.
   - Accept: in a custom tournament-draft lobby, the UI mirrors every ban, pick and hover within 1 second. A code search finds no write calls to the client.
4. **Phase 3: Data pipeline.** Rate-limited Riot client, crawler for the Iron, Gold and Emerald brackets, aggregation with shrinkage, OP.GG bootstrap.
   - Accept: current-patch stats exist for every champion and role with 200+ games. A coverage report lists sparse matchups.
5. **Phase 4: Pools + logging.** Text pool import, Pools page, post-game match fetch and logging, History page.
   - Accept: after a real game, it appears in History with the correct result, side and every action.
6. **Phase 5: Learning (v2).** Comfort re-scoring with accept/lock, draft report, style suggestion, Prep page.
   - Accept: tiers update after each logged game. The report shows our predicted win % against the result.
7. **Phase 6: Model + scouting (v2).** Logistic regression, lookahead, enemy scouting if the day-one fixture shows it's possible.
   - Accept: on held-out matches, the trained model beats the v1 scorer's Brier score.

**Instructions for Claude Code:** work one phase at a time. Write tests alongside the code. Never add write calls to the League client. Ask before adding dependencies outside the stack table.

## Testing and evaluation

The engine and parser are tested offline against fixtures. The model is judged on calibration, not on single games.

- **Engine invariants:** never suggests an unavailable champion; respects filled roles; follows the draft order for both sides and both formats; is deterministic for the same input.
- **LCU replay:** recorded session JSON in `fixtures/` replays through the parser and must produce the same `DraftState` sequence. Re-record after client patches.
- **Data:** aggregation tests on a small sample of known matches; shrinkage leaves large samples nearly unchanged.
- **Model:** Brier score and a calibration table (predicted vs. actual win rate in 10-point bins) on held-out crawled matches for each bracket.
- **Our games:** log predicted win % against results. With under 50 games, report the trend only; small samples will swing wildly.

## Compliance, risks and open questions

This is a private, read-only advisory tool. Keeping it that way is the main compliance safeguard.

**Compliance**

- **Riot API key:** create an account at [developer.riotgames.com](https://developer.riotgames.com/). The development key needs renewing daily, which is fine for building. Then apply for a personal key for ongoing personal use. Read the developer policies before applying.
- **LCU:** unofficial and unsupported. Read-only access only; no automated hovering, picking or locking.
- **OP.GG MCP:** bootstrap only, cached. No published rate limits or commercial terms were found, so keep usage light.
- **Data:** crawled stats stay on our machine; nothing is redistributed.

**Risks**

| Risk | Mitigation |
| --- | --- |
| Ranked 5s ends on 7 Jan 2027 | Draft format is config; Flex and Clash work unchanged |
| A handful of team games is too few to learn from | Manual tiers as priors, shrinkage, solo/flex history counted toward comfort |
| Client patch breaks the parser | Parser isolated in one module; fixture tests fail loudly |
| Rate limits slow the crawl | Nightly runs, cache, current patch only |
| Enemy hidden until load | Meta-based enemy model; scouting only if the fixture shows it |

**Open questions**

- [ ] Riot IDs for all five players
- [ ] Champion pools as text, exported from Machine Loling in the import format
- [ ] Ranked 5s queue ID (read from `/lol-gameflow/v1/session` in the first game)
- [ ] What the scouting tooltip and `theirTeam` expose (day-one fixture dump)
- [ ] Whether Iron-bracket stats are too noisy for the support role, or a Silver floor works better

## Appendix

**Tournament draft order** (B = blue, R = red; [Bayes Esports pick/ban docs](https://docs.bayesesports.com/docs-data-portal/misc/pick_ban_misses)).

| Phase | Steps | Order |
| --- | --- | --- |
| Ban 1 | 1–6 | B R B R B R |
| Pick 1 | 7–12 | B R R B B R |
| Ban 2 | 13–16 | R B R B |
| Pick 2 | 17–20 | R B B R |

**Prototype.** `draft_engine.py` (shared earlier in our chat) holds the v1 scorer, ban/pick ranking, enemy softmax and a full simulated draft on toy data. Port it; don't import it.

**Sources**

- [Riot support: Ranked 5s](https://support.riotgames.com/en-us/league-of-legends/events/ranked-5s)
- [League of Legends wiki: Team drafting](https://wiki.leagueoflegends.com/en-us/Blind_pick)
- [Bayes Esports: picks and bans](https://docs.bayesesports.com/docs-data-portal/misc/pick_ban_misses)
- [OP.GG MCP server listing](https://lobehub.com/en/mcp/opgginc-opgg-mcp)
- [lcu-driver champ select example](https://gist.github.com/mrtolkien/68b9f37546f3f1b6ba27f47fa91f66c4)
- [LCU API plugin reference](https://skillsmp.com/creators/hoangvu12/ame/claude-skills-lcu-api)
