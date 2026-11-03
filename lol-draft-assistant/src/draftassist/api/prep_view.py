"""The merged Prep page's top half (docs/synergy-v2.md, Stage 4): visualiser, plan panel, synergy table.

- Columns: our five role pools (main, comfortable, learning; up to 15), or hand-edited lists.
- Scope: "lane" draws the core pairs only (TOP-JG, JG-MID, ADC-SUP); "teamfight" every cross-role pair.
- View: the value each edge is coloured by. Overall = the pair's Lane score (in-lane scope) or its part of
  Overall (teamfight scope); Stat; Style. Team has no edges: it lives in the plan panel.
- Threshold: edges with |value| below it are left out.
- Locks build the plan: a locked node collapses its column; every other available node gets a chip with
  its Overall gain if added to the plan.
- Table: every core pair from the columns, plus (from two locks) the best single-champion completions.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from itertools import combinations, product

from draftassist.api.synergy import default_columns
from draftassist.api.synergy_v2 import pair_json, pp, rules_json, team_json
from draftassist.data.provider import ChampionCatalog
from draftassist.engine.comp import EMPTY_PROFILES, ChampionProfiles, badge
from draftassist.engine.model import ROLE_LABELS, ROLES, OurTeam
from draftassist.engine.synergy import LOW_DATA_GAMES
from draftassist.engine.synergy_v2 import CORE_PAIRS, SynergyEvaluator

SCOPES = ("lane", "teamfight")
VIEWS = ("overall", "stat", "style", "team")
MAX_PER_ROLE = 15
COMPLETIONS = 10


def _edge_value(p, scope: str, view: str) -> float | None:
    if view == "stat":
        return p.stat
    if view == "style":
        return p.style
    return p.lane if scope == "lane" else p.contribution


def build_prep_view(ev: SynergyEvaluator, catalog: ChampionCatalog, team: OurTeam, scope: str = "teamfight",
                    view: str = "overall", threshold_pp: float = 0.5, locks: Mapping[str, int] | None = None,
                    columns: Mapping[str, list[int]] | None = None, unavailable: Iterable[int] = (),
                    players: Mapping[str, str] | None = None, enemy: list[tuple[int, str]] | None = None,
                    profiles: ChampionProfiles = EMPTY_PROFILES) -> dict:
    if scope not in SCOPES or view not in VIEWS:
        raise ValueError(f"scope must be one of {SCOPES}, view one of {VIEWS}")
    locks = {r: c for r, c in (locks or {}).items() if c}
    unavailable = set(unavailable) - set(locks.values())
    defaults = default_columns(team, ROLES, MAX_PER_ROLE)
    cols = {r: list(dict.fromkeys((columns or {}).get(r) or defaults[r]))[:MAX_PER_ROLE] for r in ROLES}
    for r, c in locks.items():
        cols[r] = [c]  # a lock collapses the column (and may add an off-pool champion)
    enemy = enemy or []
    players = players or {}

    plan = ev.evaluate(locks, enemy)
    base = plan.overall

    def gain(role: str, c: int) -> float | None:
        if not locks or role in locks or c in unavailable:
            return None
        return ev.evaluate({**locks, role: c}, enemy, full=False).overall - base

    def node(role: str, c: int) -> dict:
        entry = team.entry(c, role)
        g = gain(role, c)
        return {**catalog.card(c), "role": role, "tier": entry.tier if entry else None, "locked": locks.get(role) == c,
                "unavailable": c in unavailable, "untagged": ev.traits.untagged(c), "gain_pp": pp(g),
                "badge": badge(profiles, c, role).as_dict()}

    out_cols = [{"role": r, "label": ROLE_LABELS[r], "player": players.get(r) or None,
                 "nodes": [node(r, c) for c in cols[r]]} for r in ROLES]

    edges = []
    if view != "team":
        role_pairs = CORE_PAIRS if scope == "lane" else list(combinations(ROLES, 2))
        for ra, rb in role_pairs:
            for a, b in product(cols[ra], cols[rb]):
                p = ev.pair(ra, a, rb, b)
                value = _edge_value(p, scope, view)
                if value is None or abs(25 * value) < threshold_pp:
                    continue
                edges.append({"a": {"role": p.role_a, "id": p.a}, "b": {"role": p.role_b, "id": p.b},
                              "core": p.core, "stat_pp": pp(p.stat), "style_pp": pp(p.style), "lane_pp": pp(p.lane),
                              "overall_pp": pp(p.contribution), "value_pp": pp(value), "games": p.games,
                              "low_data": p.low_data, "rules": rules_json(p.rules)})

    rows = []
    for ra, rb in CORE_PAIRS:
        for a, b in product(cols[ra], cols[rb]):
            if a in unavailable or b in unavailable:
                continue
            p = ev.pair(ra, a, rb, b)
            rows.append({"a": {**catalog.card(p.a), "role": p.role_a}, "b": {**catalog.card(p.b), "role": p.role_b},
                         "pair": f"{ROLE_LABELS[p.role_a]}–{ROLE_LABELS[p.role_b]}", "stat_pp": pp(p.stat),
                         "style_pp": pp(p.style), "lane_pp": pp(p.lane), "overall_pp": pp(p.contribution),
                         "games": p.games, "low_data": p.low_data,
                         "why": rules_json(p.rules)[0]["label"] if p.rules else None})
    completions = []
    if len(locks) >= 2:
        pools = {r: cols[r] for r in ROLES if r not in locks}
        sugg = ev.suggest(locks, pools, unavailable, enemy, k=COMPLETIONS)
        flat = sorted((s for ss in sugg.values() for s in ss), key=lambda s: (-s.gain, s.champ))[:COMPLETIONS]
        for s in flat:
            r = s.result
            why = None
            added = [p for p in r.pairs if s.champ in (p.a, p.b) and p.rules]
            if added:
                best = max(added, key=lambda p: abs(p.rules[0][1]))
                why = rules_json(best.rules)[0]["label"]
            completions.append({"champ": {**catalog.card(s.champ), "role": s.role}, "role": s.role,
                                "gain_pp": pp(s.gain), "stat_pp": pp(r.stat_total), "style_pp": pp(r.style_total),
                                "team_pp": pp(r.team), "overall_pp": pp(r.overall), "why": why})

    locked = [(r, locks[r]) for r in ROLES if r in locks]
    return {
        "scope": scope, "view": view, "threshold_pp": threshold_pp, "window": ev.stat.window,
        "low_data_games": LOW_DATA_GAMES, "columns": out_cols, "edges": edges,
        "plan": {"champs": [{**catalog.card(c), "role": r} for r, c in locked],
                 "pairs": [pair_json(p, catalog.card) for p in plan.pairs], "team": team_json(plan)},
        "table": {"pairs": rows, "completions": completions},
    }

