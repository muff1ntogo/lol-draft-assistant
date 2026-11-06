"""The Matchup tree (docs/matchup-archetypes.md, Stage 7). Pure, no I/O.

Per role: each enemy archetype with its most-picked champions, up to 3 answers (our player's best if their pool covers it,
then the best from outside the pool), the archetype-level expectation, and a gap flag. The coverage strip is the same rows as
one chip per enemy archetype: chips and gaps are about the pool alone.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from draftassist.engine.blind import GAP_PP, PICK_FLOOR

SHOWN_CHAMPS = 5
SCORED_CHAMPS = 8  # an archetype's most-picked champions an answer is scored on
SHOWN_ANSWERS = 3
# A counter does this much better into an archetype than its own average, beyond what the role's other champions gain there
# (an archetype can be easy for everyone).
COUNTER_PP = 0.5
GREEN_PP = 0.0
MIN_CHIP_CONFIDENCE = 0.01  # of the best answer's matchups, below this the chip says "no data"


class StatsLaneData:
    """A lane answer read from any set of stats, blended as usual."""

    def __init__(self, stats, lane_weight: float):
        self._stats, self._weight = stats, lane_weight

    def matchup_wr(self, role: str, ours: int, theirs: int) -> float | None:
        return self._stats.blended_wr(role, ours, theirs, self._weight)


def chip(best_pp: float | None, confidence: float = 1.0) -> str:
    """A gap is flagged even when it's only an estimate. Covered or thin needs games behind it: with none, the answer is
    the archetype prior, which sits near 0 whatever the real matchup is."""
    if best_pp is None or best_pp < GAP_PP:
        return "red"
    if confidence < MIN_CHIP_CONFIDENCE:
        return "grey"
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


def _strongest_first(answers: list[dict]) -> list[dict]:
    return sorted(answers, key=lambda a: (-a["pp"], a["champ"]))


def _shortlist(pool: list[dict], outside: list[dict]) -> list[dict]:
    """The pool's best answer if it covers the archetype, then the strongest from outside the pool. Room for one counter:
    the biggest one, tagged, taking the last place if it didn't earn a place anyway. It has to beat the archetype outright,
    on games, not just suffer less than usual. With no one outside the pool to show, the pool fills the list."""
    pool, outside = _strongest_first(pool), _strongest_first(outside)
    ours = [pool[0]] if pool and chip(pool[0]["pp"], pool[0]["confidence"]) == "green" else []
    shown = outside[:SHOWN_ANSWERS - len(ours)]
    counters = [a for a in ours + outside if a["counter_pp"] >= COUNTER_PP and a["pp"] >= 0
                and a["confidence"] >= MIN_CHIP_CONFIDENCE]
    counter = max(counters, key=lambda a: (a["counter_pp"], -a["champ"]), default=None)
    if counter is not None:
        counter["counter"] = True
        if counter not in ours and counter not in shown:
            shown[-1] = counter
    return ours + shown if ours or shown else pool[:SHOWN_ANSWERS]


def matchup_tree(engine, role: str, words: Sequence[str], plans: Mapping[str, str] | None = None,
                 expected=None, stats=None, bot=None) -> list[dict]:
    """One row per enemy archetype. `expected(a, b)`: the archetype-level pp of a against b, from the fit. `stats`: what
    to read matchups and pick rates from (default: the engine's, for the current patch). `bot`: the 2v2 tables that go with
    them (default: the engine's)."""
    stats = stats if stats is not None else engine.our_stats(role)
    pool = engine.team.pools.get(role, {})
    labels = engine.data.labels or {}
    data = engine.resp_data(frozenset(), base=StatsLaneData(stats, engine.cfg.lane_weight), bot=bot)
    field = set(pool) | {c for c, s in stats.meta.items() if role in s and s[role].pick_rate >= PICK_FLOOR}
    every = {}
    for archetype in words:
        scored = _members(engine, stats, role, archetype)[:SCORED_CHAMPS]
        every[archetype] = [a for c in sorted(field) if (a := _answer(engine, stats, role, c, scored, data)) is not None]
    by_champ: dict[int, list[float]] = {}
    for answers in every.values():
        for a in answers:
            by_champ.setdefault(a["champ"], []).append(a["pp"])
    rows = []
    for archetype in words:
        members = _members(engine, stats, role, archetype)
        scored = members[:SCORED_CHAMPS]
        own = {c: sum(pps) / len(pps) for c, pps in by_champ.items()}
        here = every[archetype]
        field_gain = sum(a["pp"] - own[a["champ"]] for a in here) / len(here) if here else 0.0
        for a in here:
            ours = labels.get((a["champ"], role))
            a |= {"own_pp": round(own[a["champ"]], 1), "counter_pp": round(a["pp"] - own[a["champ"]] - field_gain, 1),
                  "counter": False, "in_pool": a["champ"] in pool,
                  "expectation": round(expected(ours, archetype), 2) if expected and ours and ours != archetype else 0.0}
        from_pool = _strongest_first([a for a in here if a["in_pool"]])
        answers = _shortlist(from_pool, [a for a in here if not a["in_pool"]])
        best = from_pool[0] if from_pool else None  # the chip, the gap and the ban candidates are about the pool
        expectation = best["expectation"] if best else None
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
            "best": best, "answers": answers, "expectation": expectation,
            "gap": bool(members) and (best is None or best["pp"] < GAP_PP),
            "chip": "none" if not members else chip(best["pp"], best["confidence"]) if best else chip(None),
            "answering": answering,
            "ban_candidates": [{"champ": c, "name": engine.data.name(c), "pp": round(pp, 1)} for pp, c in ban_candidates],
        })
    return rows
