"""Prep sheet (spec, Core workflows: pre-session prep): priority bans per side, each player's best 3
this patch (comfort and meta blended as in the engine), safe blind first picks, and flex picks.

Side-specific bans: blue picks first and is counter-picked in every round, so its bans lean towards
protecting our pools (Matchup); red holds the counter-pick, so its bans lean towards denying meta
power picks (Threat). Each side moves the configured weights by SIDE_SHIFT, keeping their sum.
"""
from __future__ import annotations

from dataclasses import replace

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import ROLE_LABELS, ROLES, BanWeights
from draftassist.engine.state import SKIPPED, DraftState

SIDE_SHIFT = 0.5
BLIND_SAFE_RISK = 0.10  # log-odds: the best open counter wins its lane by under ~2.5pp


def _side_engine(engine: DraftEngine, side: str) -> DraftEngine:
    w = engine.cfg.ban_weights
    shift = SIDE_SHIFT if side == "blue" else -SIDE_SHIFT
    moved = min(max(shift, -w.matchup), w.threat)  # never below zero
    weights = BanWeights(threat=w.threat - moved, matchup=w.matchup + moved, comp=w.comp)
    return DraftEngine(engine.data, engine.team, replace(engine.cfg, ban_weights=weights))


def prep_sheet(engine: DraftEngine, card) -> dict:
    """`card(champ_id)` -> display dict."""
    bans = {}
    for side in ("blue", "red"):
        eng = _side_engine(engine, side)
        bans[side] = {"weights": vars(eng.cfg.ban_weights),
                      "bans": [{"champ": card(s.champ_id), "reason": s.reason, "value": round(s.value, 4)}
                               for s in eng.suggest_bans(DraftState(TOURNAMENT, side), k=5)]}

    best = []
    for role in ROLES:
        pool = engine.team.pools.get(role, {})
        bracket = engine.our_stats(role)
        ranked = sorted(pool, key=lambda c: (-engine.pick_weight(c, role), c))
        rows = []
        for c in ranked[:3]:
            stat = bracket.role_stat(c, role)
            rows.append({"champ": card(c), "tier": pool[c].tier, "comfort": round(pool[c].comfort, 2),
                         "win_rate": round(stat.win_rate, 3) if stat else None,
                         "score": round(engine.pick_weight(c, role), 3)})
        best.append({"role": role, "role_label": ROLE_LABELS[role], "bracket": engine.team.brackets[role], "best": rows})

    first_pick = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6)  # blue's first pick: fully exposed
    pool_champs = engine.team.pool_champs()
    candidates = [s for s in engine.suggest_picks(first_pick, k=10_000) if s.champ_id in pool_champs]
    blind = [{"champ": card(s.champ_id), "role_label": ROLE_LABELS[s.role], "win_prob": round(s.win_prob, 4),
              "counter_risk": round(s.counter_risk, 3), "worst_counter": card(s.counter_id) if s.counter_id else None}
             for s in candidates if s.counter_risk <= BLIND_SAFE_RISK][:5]

    flex = []
    for c in sorted(pool_champs):
        roles = engine.team.roles_for(c)
        if len(roles) >= 2:
            flex.append({"champ": card(c), "roles": [ROLE_LABELS[r] for r in roles],
                         "tiers": {ROLE_LABELS[r]: engine.team.pools[r][c].tier for r in roles}})

    return {"bans": bans, "best": best, "blind_safe": blind, "flex": flex,
            "notes": {"bans": "Blue leans towards protecting our pools (it is counter-picked); red towards denying "
                              "meta power picks (it holds the counter-pick).",
                      "blind_safe": "Pool champions whose best open counter wins its lane by under ~2.5pp, "
                                    "ranked by win probability as blue's first pick."}}
