"""Blind-pick safety and pool health (docs/matchup-archetypes.md, Stage 6). Pure, no I/O.

    blind_safety = comfort + w * z(matchup) + w * z(synergy) + w * z(worst counter) + w * z(behind) + w * z(kit)

Each part is a z-score within the role, over its real picks and our pool, so no part dominates by its scale:
- matchup: the mean of our lane and game blend against likely opponents, weighted by pick rate;
- synergy: the mean synergy with our current pools, weighted by tier (read when the engine loads, so it follows them);
- worst counter: the pick-rate-weighted mean of the worst 3 of the role's top 20 opponents;
- behind: win rate when 300+ gold behind at 15 minutes;
- kit: ability range and escape.
A part with no data counts as average.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from draftassist.engine.bot_lane import PARTNER_TIER_WEIGHT
from draftassist.engine.mathx import logit
from draftassist.engine.model import ROLES
from draftassist.engine.traits import STRONG_TAG

PICK_FLOOR = 0.005  # opponents and role champions picked less than this don't count
TOP_OPPONENTS, WORST_OF = 20, 3
COMFORT_SCALE = 0.3  # the tier comfort is +-0.3 or so: brings it to about the size of a z-score
PARTS = ("matchup", "synergy", "worst", "behind", "kit")
BLIND_SAFE = 0.0  # a total at or above this is blind-safe: at least average for the role, comfort included
GAP_PP = -1.5  # a best answer below this is a gap
ROLES_WITHOUT_SETUP = ("adc",)  # "where the role allows"


@dataclass(frozen=True)
class BlindScore:
    champ: int
    role: str
    total: float
    comfort: float
    parts: Mapping[str, float]  # part -> z-score
    raw: Mapping[str, float | None]


def _opponents(engine, role: str, champ: int) -> list[tuple[int, float]]:
    enemy = engine.enemy_stats
    rates = [(c, s[role].pick_rate) for c, s in enemy.meta.items() if role in s and s[role].pick_rate >= PICK_FLOOR]
    return [(c, r) for c, r in sorted(rates, key=lambda t: (-t[1], t[0])) if c != champ]


def _raw(engine, role: str, champ: int, data) -> dict[str, float | None]:
    opponents = _opponents(engine, role, champ)
    values = []
    for opp, rate in opponents:
        wr = data.matchup_wr(role, champ, opp)
        values.append((logit(wr) if wr is not None else 0.0, rate))
    total = sum(r for _, r in values)
    matchup = sum(v * r for v, r in values) / total if total else None
    worst_pool = sorted(values[:TOP_OPPONENTS])[:WORST_OF]
    worst_rate = sum(r for _, r in worst_pool)
    worst = sum(v * r for v, r in worst_pool) / worst_rate if worst_rate else None

    syn = 0.0
    for other in ROLES:
        if other == role:
            continue
        pool = engine.team.pools.get(other, {})
        weight = sum(PARTNER_TIER_WEIGHT[e.tier] for c, e in pool.items() if c != champ)
        for partner, e in pool.items():
            if partner != champ and weight:
                syn += PARTNER_TIER_WEIGHT[e.tier] / weight * engine.pair_synergy(role, champ, other, partner)[0]

    traits = engine.data.traits
    behind = kit = None
    if traits is not None:
        behind = (traits.measured.get((champ, role)) or {}).get("behind_value")
        if traits.kit is not None:
            reach, escape = traits.kit.intensity(champ, role, "ability_range"), traits.kit.intensity(champ, role, "escape")
            if reach is not None and escape is not None:
                kit = (reach + escape) / 2
    return {"matchup": matchup, "synergy": syn, "worst": worst, "behind": behind, "kit": kit}


def _z(values: Mapping[int, float | None]) -> dict[int, float]:
    known = [v for v in values.values() if v is not None]
    if len(known) < 2:
        return dict.fromkeys(values, 0.0)
    mean = sum(known) / len(known)
    sd = (sum((v - mean) ** 2 for v in known) / len(known)) ** 0.5
    return {c: 0.0 if v is None or sd <= 0 else (v - mean) / sd for c, v in values.items()}


def blind_table(engine, role: str) -> dict[int, BlindScore]:
    """Every real pick in the role and our pool, scored. Built once per engine: an engine is rebuilt on pool changes."""
    cache = engine.blind_cache
    if role in cache:
        return cache[role]
    stats = engine.our_stats(role)
    pool = set(engine.team.pools.get(role, {}))
    champs = pool | {c for c, s in stats.meta.items() if role in s and s[role].pick_rate >= PICK_FLOOR}
    data = engine.resp_data(frozenset())
    raw = {c: _raw(engine, role, c, data) for c in sorted(champs)}
    z = {p: _z({c: raw[c][p] for c in raw}) for p in PARTS}
    weights = engine.cfg.blind_weights
    out = {}
    for c in raw:
        comfort = engine.comfort_of(c, role) / COMFORT_SCALE
        parts = {p: z[p][c] for p in PARTS}
        total = weights.get("comfort", 1.0) * comfort + sum(weights.get(p, 1.0) * parts[p] for p in PARTS)
        out[c] = BlindScore(c, role, total, comfort, parts, raw[c])
    cache[role] = out
    return out


def blind_safety(engine, champ: int, role: str) -> float:
    score = blind_table(engine, role).get(champ)
    return score.total if score else 0.0


def pool_health(engine, role: str) -> dict:
    """The drafting guide's ideal pool: a strong blind pick, a setup champion where the role allows, and a counter to the
    main's worst matchups. What's present and what's missing. Informational."""
    pool = engine.team.pools.get(role, {})
    name = engine.data.name
    table = blind_table(engine, role)
    ranked = sorted((c for c in pool if c in table), key=lambda c: (-table[c].total, c))
    best = ranked[0] if ranked else None
    blind = {"champ": best, "name": name(best) if best else None, "score": round(table[best].total, 2) if best else None,
             "ok": bool(best is not None and table[best].total >= BLIND_SAFE)}

    traits = engine.data.traits
    if role in ROLES_WITHOUT_SETUP:
        setup = {"champ": None, "name": None, "ok": True, "applies": False}
    else:
        def setup_of(c):
            e = traits.tag_entry(c, role) if traits is not None else None
            return e.raw("setup") if e is not None else 0.0

        strongest = max(pool, key=lambda c: (setup_of(c), -c), default=None)
        has = strongest is not None and setup_of(strongest) >= STRONG_TAG
        setup = {"champ": strongest if has else None, "name": name(strongest) if has else None, "ok": has, "applies": True}

    tier_rank = {"main": 0, "comfortable": 1, "learning": 2}
    main = min(pool, key=lambda c: (tier_rank[pool[c].tier], -pool[c].comfort, c), default=None)
    counter = {"main": name(main) if main else None, "worst": [], "ok": main is None}
    if main is not None:
        data = engine.resp_data(frozenset())
        pp = lambda ours, theirs: 100 * ((data.matchup_wr(role, ours, theirs) or 0.5) - 0.5)
        opponents = [o for o, _ in _opponents(engine, role, main)[:TOP_OPPONENTS]]
        for opp in sorted(opponents, key=lambda o: (pp(main, o), o))[:WORST_OF]:
            answers = [(pp(c, opp), c) for c in pool if c not in (main, opp)]
            answer = max(answers, default=None)
            answered = answer is not None and answer[0] > pp(main, opp) and answer[0] >= GAP_PP
            counter["worst"].append({"name": name(opp), "main_pp": round(pp(main, opp), 1), "answered": answered,
                                     "answer": name(answer[1]) if answered else None,
                                     "answer_pp": round(answer[0], 1) if answered else None})
        counter["ok"] = any(w["answered"] for w in counter["worst"]) or not counter["worst"]
    return {"role": role, "blind": blind, "setup": setup, "counter": counter}
