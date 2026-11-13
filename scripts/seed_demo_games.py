"""Seed a made-up roster and simulated games into a SCRATCH database, so Review (History, Team trends) and the Roster
pages (lineup, bench, profiles) have something to show in demo mode. For checking the UI only
(docs/handoff-ui-redesign.md): every player, game, rank and mastery figure here is invented, and the Riot IDs end in
#DEMO so they can't be mistaken for anyone.

    DRAFTASSIST_HOME=<scratch> DRAFTASSIST_DB=<scratch>/demo.sqlite DRAFTASSIST_LCU=off DRAFTASSIST_DEMO_LOG=1 \
        uv run python scripts/seed_demo_games.py

Run it once, with the app stopped, after the app has started once on that folder (it loads the champions on first
start). It refuses to run unless DRAFTASSIST_DB is set, so it can't write into a real database by accident. This is a
script and not part of demo mode on purpose: demo mode reads whatever database the app is pointed at, and must never
write invented players into a real one.
"""
import json
import os
import random
import sys
from datetime import UTC, datetime, timedelta

if not os.environ.get("DRAFTASSIST_DB"):
    sys.exit("Set DRAFTASSIST_DB to a scratch database first (see this file's docstring).")

from sqlmodel import Session, select  # noqa: E402

from draftassist.api.main import build_services  # noqa: E402
from draftassist.config import load_settings  # noqa: E402
from draftassist.data.pools import set_players, swap_player  # noqa: E402
from draftassist.db.models import Draft, GameExecution, GameExecutionRole, Player, PlayerGame, PoolEntry  # noqa: E402
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT  # noqa: E402
from draftassist.engine.model import ROLES  # noqa: E402
from draftassist.lcu.demo import demo_sessions  # noqa: E402
from draftassist.learning.comfort import rescore  # noqa: E402
from draftassist.learning.execution import label as z_label  # noqa: E402
from draftassist.learning.execution import verdict  # noqa: E402
from draftassist.learning.history import collect_logged  # noqa: E402

GAMES = 26
DAY_MS = 86_400_000
TIERS = ("GOLD", "PLATINUM", "EMERALD", "SILVER")
rng = random.Random(7)
svc = build_services(load_settings())
now = datetime.now(UTC)
now_ms = int(now.timestamp() * 1000)


def eog(game_id: int, side: str, won: bool) -> dict:
    team = 100 if side == "blue" else 200
    return {"gameId": game_id, "gameLength": rng.randint(1500, 2400),
            "teams": [{"teamId": team, "isPlayerTeam": True, "isWinningTeam": won},
                      {"teamId": 300 - team, "isPlayerTeam": False, "isWinningTeam": not won}]}


def stat_line(stat: str, z: float, sd: float, expected: float = 0.0) -> dict:
    return {"stat": stat, "actual": expected + z * sd, "expected": expected, "sd": sd, "z": z, "label": z_label(z)}


# ---------- the roster: five active players and two on the bench ----------
with Session(svc.db) as s:
    if s.exec(select(Player).where(Player.riot_id != "")).first() is not None:
        sys.exit("This database already has players: seed a fresh scratch folder instead.")
pools = {r: [c for c in svc.engine.team.pools.get(r, {})][:5] for r in ROLES}  # the placeholder pools: each role's most picked
if not all(pools.values()):
    sys.exit("No champion stats yet: start the app once on this folder first, then stop it.")
set_players(svc.db, [{"role": r, "riot_id": f"Demo {r.title()}#DEMO"} for r in ROLES])
swap_player(svc.db, "mid", "Demo Sub#DEMO")  # "Demo Mid" goes to the bench...
swap_player(svc.db, "mid", "Demo Mid#DEMO")  # ...and comes back: "Demo Sub" waits on the bench with a mid pool
swap_player(svc.db, "support", "Demo Newcomer#DEMO")  # a benched player with nothing known about them
swap_player(svc.db, "support", "Demo Support#DEMO")

with Session(svc.db) as s:
    players = {p.riot_id: p for p in s.exec(select(Player)).all() if p.riot_id}
    for n, (riot_id, p) in enumerate(sorted(players.items())):
        if "Newcomer" in riot_id:
            continue
        champs = pools[p.role]
        for i, champ in enumerate(champs if "Sub" not in riot_id else champs[1:4]):
            s.add(PoolEntry(puuid=p.puuid, champ_id=champ, role=p.role, manual_tier=("main", "comfortable", "comfortable", "learning", "learning")[i]))
        # solo and flex ranks, and mastery: mostly their pool, plus two champions they don't play any more
        p.rank_json = json.dumps([
            {"queueType": "RANKED_SOLO_5x5", "tier": TIERS[n % 4], "rank": ("II", "IV", "I", "III")[n % 4], "leaguePoints": 12 + 9 * n, "wins": 40 + n, "losses": 37},
            {"queueType": "RANKED_FLEX_SR", "tier": TIERS[(n + 1) % 4], "rank": "III", "leaguePoints": 55, "wins": 18, "losses": 15}])
        others = [c for c in pools[ROLES[(ROLES.index(p.role) + 1) % 5]][:2]]
        p.mastery_json = json.dumps(sorted(
            [[c, 320_000 - 60_000 * i + 1_000 * n, now_ms - (2 + 3 * i) * DAY_MS] for i, c in enumerate(champs)]
            + [[c, 140_000 - 50_000 * i, now_ms - 300 * DAY_MS] for i, c in enumerate(others)], key=lambda m: -m[1]))
        p.profile_fetched_at = now
        p.api_puuid = f"demo-{n}"
        # solo and flex games over the last 60 days: mostly their role and pool, some elsewhere, a few without figures
        for g in range(46 if p.active else 18):
            off_role = g % 6 == 5
            role = ROLES[(ROLES.index(p.role) + 2) % 5] if off_role else p.role
            champ = rng.choice(pools[role][:2]) if off_role else rng.choices(champs + [pools[role][-1]], weights=(9, 6, 4, 2, 1, 1)[:len(champs) + 1])[0]
            minutes = rng.randint(22, 38)
            figures = {} if g % 11 == 10 else {
                "kills": rng.randint(1, 11), "deaths": rng.randint(1, 8), "assists": rng.randint(2, 14),
                "cs": int(minutes * (1.2 if role == "support" else rng.uniform(6.0, 8.4))),
                "vision": int(minutes * (2.3 if role == "support" else rng.uniform(0.6, 1.2))), "duration_s": minutes * 60}
            s.add(PlayerGame(puuid=p.puuid, match_id=f"DEMO_{n}_{g}", champ_id=champ, role=role, win=rng.random() < 0.5 + 0.03 * (n % 3),
                             queue_id=420 if g % 3 else 440, game_start_ms=now_ms - int((g * 1.25 + rng.random()) * DAY_MS), source="match-v5", **figures))
        s.add(p)
    s.commit()
svc.reload()

# ---------- team games: simulated drafts, logged as the app logs them ----------
ids = []
for i in range(GAMES):
    side = "blue" if i % 2 == 0 else "red"
    won = rng.random() < 0.56
    game_id = 7_400_000_000 + i
    svc.live.handle_phase("ChampSelect", None)
    for event in demo_sessions(svc.engine, TOURNAMENT if i % 3 else STANDARD_RANKED, side, game_id=game_id):
        svc.live.handle_session(event)
    svc.live.handle_phase("InProgress", None)
    svc.live.handle_phase("EndOfGame", None)
    ids.append((svc.live.handle_end_of_game(eog(game_id, side, won)), won))
    svc.live.handle_phase("Lobby", None)

with Session(svc.db) as s:
    for n, (draft_id, won) in enumerate(ids):
        d = s.get(Draft, draft_id)
        d.played_at = now - timedelta(days=GAMES - n, hours=rng.randint(0, 5))
        s.add(d)
        if n % 6 == 5:
            continue  # some games have no timeline: draft only
        p0 = min(0.62, max(0.4, (d.pred_win_prob or 0.5) * 0.2 + 0.4 + rng.uniform(-0.04, 0.04)))
        p15 = min(0.9, max(0.1, p0 + rng.uniform(-0.25, 0.25) + (0.08 if won else -0.08)))
        early, conversion = p15 - p0, (1.0 if won else 0.0) - p15
        shares = {r: rng.uniform(-0.06, 0.07) for r in ("top", "jungle", "mid", "bot", "adc", "support")}
        shares["objectives"] = early - sum(shares[r] for r in ("top", "jungle", "mid", "bot"))
        top2 = sorted(("top", "jungle", "mid", "bot"), key=lambda r: -abs(shares[r]))[:2]
        s.add(GameExecution(
            draft_id=draft_id, window="demo", p0=p0, p15=p15, draft=p0 - 0.5, early=early, conversion=conversion,
            verdict=verdict(p0, p15, early, won), contributors_json=json.dumps([f"{r.title()} {z_label(shares[r] * 30)}" for r in top2]),
            detail_json=json.dumps({"deaths_while_ahead": [rng.randint(0, 5), rng.randint(0, 5)], "barons": [rng.randint(0, 2), rng.randint(0, 2)],
                                    "dragons_after_15": [rng.randint(0, 3), rng.randint(0, 3)], "towers_after_15": [rng.randint(0, 8), rng.randint(0, 8)],
                                    "gold_25": stat_line("gold_25", rng.uniform(-1.5, 1.5), 2400)})))
        for role, share in shares.items():
            z = share * 30
            s.add(GameExecutionRole(draft_id=draft_id, role=role, share=share, label=z_label(z), headline_json=json.dumps(stat_line("gold", z, 620)),
                                    stats_json=json.dumps([stat_line("gold", z, 620), stat_line("xp", z * 0.8, 480), stat_line("cs", z * 0.6, 9.5)])))
    s.commit()

logged = collect_logged(svc.db)  # the team games become each active player's games, as the learning job files them
proposals = rescore(svc.db)
svc.save_roster()
print(f"Seeded {len(players)} players, {GAMES} team games ({logged} player games from them), {len(proposals)} tier suggestions.")
