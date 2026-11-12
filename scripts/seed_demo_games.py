"""Seed simulated games into a SCRATCH database, so Review (History, Team trends) and the roster profiles have
something to show in demo mode. For checking the UI only (docs/handoff-ui-redesign.md): the games, their execution
splits and the verdict names are made up.

    DRAFTASSIST_HOME=<scratch> DRAFTASSIST_DB=<scratch>/demo.sqlite DRAFTASSIST_LCU=off DRAFTASSIST_DEMO_LOG=1 \
        uv run python scripts/seed_demo_games.py

Run it with the app stopped, after the app has started once on that folder (it loads the champions on first start).
It refuses to run unless DRAFTASSIST_DB is set, so it can't write into a real database by accident.
"""
import os
import sys

if not os.environ.get("DRAFTASSIST_DB"):
    sys.exit("Set DRAFTASSIST_DB to a scratch database first (see this file's docstring).")

import json
import random
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from draftassist.api.main import build_services
from draftassist.config import load_settings
from draftassist.db.models import Draft, GameExecution, GameExecutionRole
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.lcu.demo import demo_sessions

rng = random.Random(7)
svc = build_services(load_settings())
def eog(gid, side, won):
    team = 100 if side == "blue" else 200
    return {"gameId": gid, "gameLength": rng.randint(1500, 2400),
            "teams": [{"teamId": team, "isPlayerTeam": True, "isWinningTeam": won}, {"teamId": 300 - team, "isPlayerTeam": False, "isWinningTeam": not won}]}
ids = []
for i in range(26):
    side = "blue" if i % 2 == 0 else "red"; fmt = TOURNAMENT if i % 3 else STANDARD_RANKED; won = rng.random() < 0.56; gid = 7_400_000_000 + i
    svc.live.handle_phase("ChampSelect", None)
    for e in demo_sessions(svc.engine, fmt, side, game_id=gid): svc.live.handle_session(e)
    svc.live.handle_phase("InProgress", None); svc.live.handle_phase("EndOfGame", None)
    ids.append((svc.live.handle_end_of_game(eog(gid, side, won)), won)); svc.live.handle_phase("Lobby", None)
line = lambda stat, z, sd, exp=0.0: {"stat": stat, "actual": exp + z * sd, "expected": exp, "sd": sd, "z": z, "label": "ahead" if z > 0.5 else "behind" if z < -0.5 else "even"}
with Session(svc.db) as s:
    for n, (did, won) in enumerate(ids):
        d = s.get(Draft, did); d.played_at = datetime.now(UTC) - timedelta(days=26 - n, hours=rng.randint(0, 5)); s.add(d)
        if n % 6 == 5: continue  # some games have no timeline
        p0 = min(0.62, max(0.4, (d.pred_win_prob or 0.5) * 0.2 + 0.4 + rng.uniform(-0.04, 0.04))); p15 = min(0.9, max(0.1, p0 + rng.uniform(-0.25, 0.25) + (0.08 if won else -0.08)))
        early = p15 - p0; conv = (1.0 if won else 0.0) - p15
        shares = {r: rng.uniform(-0.06, 0.07) for r in ["top", "jungle", "mid", "bot", "adc", "support"]}
        shares["objectives"] = early - sum(shares[r] for r in ["top", "jungle", "mid", "bot"])
        top2 = sorted(["top", "jungle", "mid", "bot"], key=lambda r: -abs(shares[r]))[:2]
        verdict = ("Won lanes, closed it out" if early > 0 else "Came back from behind") if won else ("Threw a lead" if early > 0 else "Lost early, never recovered")
        s.add(GameExecution(draft_id=did, window="16.22", p0=p0, p15=p15, draft=p0 - 0.5, early=early, conversion=conv, verdict=verdict,
                            contributors_json=json.dumps([f"{r} {'ahead' if shares[r] > 0 else 'behind'}" for r in top2]),
                            detail_json=json.dumps({"deaths_while_ahead": [rng.randint(0, 5), rng.randint(0, 5)], "barons": [rng.randint(0, 2), rng.randint(0, 2)],
                                                    "dragons_after_15": [rng.randint(0, 3), rng.randint(0, 3)], "towers_after_15": [rng.randint(0, 8), rng.randint(0, 8)],
                                                    "gold_25": line("gold_25", rng.uniform(-1.5, 1.5), 2400)})))
        for r, sh in shares.items():
            z = sh * 18
            s.add(GameExecutionRole(draft_id=did, role=r, share=sh, label="Wins lane" if z > 0.5 else "Loses lane" if z < -0.5 else "Even",
                                    headline_json=json.dumps(line("gold", z, 620)), stats_json=json.dumps([line("gold", z, 620), line("xp", z * 0.8, 480), line("cs", z * 0.6, 9.5)])))
    s.commit()
print("seeded", len(ids))
