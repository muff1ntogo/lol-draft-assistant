"""The Matchup tree (docs/matchup-archetypes.md, Stage 7). Pure, no I/O.

Per role: each enemy archetype with its most-picked champions, our player's best answer from their pool, the archetype-level
expectation, and a gap flag. The coverage strip is the same rows as one chip per enemy archetype.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from draftassist.engine.blind import GAP_PP, PICK_FLOOR

SHOWN_CHAMPS = 5
SCORED_CHAMPS = 8  # an archetype's most-picked champions an answer is scored on
GREEN_PP = 0.0


class StatsLaneData:
    """A lane answer read from any set of stats, blended as usual."""

    def __init__(self, stats, lane_weight: float):
        self._stats, self._weight = stats, lane_weight

    def matchup_wr(self, role: str, ours: int, theirs: int) -> float | None:
        return self._stats.blended_wr(role, ours, theirs, self._weight)


def chip(best_pp: float | None) -> str:
    if best_pp is None or best_pp < GAP_PP:
        return "red"
    return "green" if best_pp >= GREEN_PP else "amber"


def _members(engine, stats, role: str, archetype: str) -> list[tuple[int, float]]:
    labels = engine.data.labels or {}
    rows = [(c, s[role].pick_rate) for c, s in stats.meta.items()
            if role in s and s[role].pick_rate >= PICK_FLOOR and labels.get((c, role)) == archetype]
    return sorted(rows, key=lambda t: (-t[1], t[0]))


def _answer(engine, stats, role: str, ours: int, members: list[tuple[int, float]], data) -> dict | None:
    total = pp = lane = game = gold = conf = 0.0
    golds = 0.0
    for opp, rate in members:
        if opp == ours:
            continue
        wr = data.matchup_wr(role, ours, opp)
        if wr is None:
            continue
        total += rate
        pp += rate * 100 * (wr - 0.5)
        game += rate * 100 * (stats.matchup_wr(role, ours, opp) - 0.5)
        lane_wr = stats.lane_wr(role, ours, opp)
        lane += rate * 100 * ((lane_wr if lane_wr is not None else 0.5) - 0.5)
        lead = stats.lane_gold_of(role, ours, opp)
        if lead is not None:
            gold += rate * lead
            golds += rate
        conf += rate * stats.matchup_confidence(role, ours, opp)
    if not total:
        return None
    return {"champ": ours, "name": engine.data.name(ours), "pp": round(pp / total, 1), "lane_pp": round(lane / total, 1),
            "game_pp": round(game / total, 1), "gold": round(gold / golds) if golds else None,
            "confidence": round(conf / total, 2)}


def matchup_tree(engine, role: str, words: Sequence[str], plans: Mapping[str, str] | None = None,
                 expected=None, stats=None) -> list[dict]:
    """One row per enemy archetype. `expected(a, b)`: the archetype-level pp of a against b, from the fit. `stats`: what
    to read matchups and pick rates from (default: the engine's, for the current patch)."""
    stats = stats if stats is not None else engine.our_stats(role)
    pool = engine.team.pools.get(role, {})
    labels = engine.data.labels or {}
    data = engine.resp_data(frozenset(), base=StatsLaneData(stats, engine.cfg.lane_weight))
    rows = []
    for archetype in words:
        members = _members(engine, stats, role, archetype)
        scored = members[:SCORED_CHAMPS]
        answers = [a for c in sorted(pool) if (a := _answer(engine, stats, role, c, scored, data)) is not None]
        best = max(answers, key=lambda a: (a["pp"], -a["champ"]), default=None)
        ours = labels.get((best["champ"], role)) if best else None
        expectation = expected(ours, archetype) if expected and ours and ours != archetype else (0.0 if best else None)
        answering = []
        if expected:
            ranked = sorted(((expected(w, archetype), w) for w in words if w != archetype), reverse=True)
            answering = [w for pp, w in ranked[:2] if pp > 0]
        worst_first = []
        for champ, rate in scored:  # what our best answer does against each of them
            against = _answer(engine, stats, role, best["champ"], [(champ, rate)], data) if best else None
            worst_first.append((against["pp"] if against else 0.0, champ))
        ban_candidates = sorted(worst_first)[:3]
        rows.append({
            "archetype": archetype, "plan": (plans or {}).get(archetype),
            "champs": [{"champ": c, "name": engine.data.name(c), "pick_rate": round(r, 3)} for c, r in members[:SHOWN_CHAMPS]],
            "best": best, "expectation": None if expectation is None else round(expectation, 2),
            "gap": bool(members) and (best is None or best["pp"] < GAP_PP),
            "chip": "none" if not members else chip(best["pp"] if best else None),
            "answering": answering,
            "ban_candidates": [{"champ": c, "name": engine.data.name(c), "pp": round(pp, 1)} for pp, c in ban_candidates],
        })
    return rows
