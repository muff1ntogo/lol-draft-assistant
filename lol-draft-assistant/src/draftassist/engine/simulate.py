"""Simulated drafts on synthetic data, for tests and a quick demo until real stats exist (Phase 3).

CLI: `uv run python -m draftassist.engine.simulate --format tournament --side blue`
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass

from draftassist.engine.engine import DraftEngine, Suggestion
from draftassist.engine.formats import DraftFormat, Side, Step, get_format
from draftassist.engine.model import ROLE_LABELS, ROLES, BracketStats, EngineData, OurTeam, RoleStat
from draftassist.engine.state import DraftState

TEAM_BRACKETS = {"top": "gold", "jungle": "emerald", "mid": "gold", "adc": "gold", "support": "iron"}


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def synthetic_data(seed: int = 7, n_champs: int = 170,
                   names: dict[int, str] | None = None) -> tuple[EngineData, OurTeam]:
    """Deterministic fake stats shaped like the real thing: ~170 champions, 1-2 roles each,
    three brackets, dense lane matchups and sparse duo synergy. Pass `names` (champ_id -> name,
    e.g. from the champions table) to generate stats over real champion ids."""
    rng = random.Random(seed)
    ids = sorted(names) if names else sorted(rng.sample(range(1, 1000), n_champs))
    champ_roles: dict[int, list[str]] = {}
    for i, c in enumerate(ids):
        roles = [ROLES[i % len(ROLES)]]
        if rng.random() < 0.25:
            roles.append(rng.choice([r for r in ROLES if r != roles[0]]))
        champ_roles[c] = roles
    base = {c: {r: (_clamp(rng.gauss(0.5, 0.02), 0.44, 0.56), rng.uniform(0.002, 0.15 if j == 0 else 0.03))
                for j, r in enumerate(rs)} for c, rs in champ_roles.items()}

    brackets = {}
    for name in ("iron", "gold", "emerald"):
        meta = {c: {r: RoleStat(_clamp(wr + rng.gauss(0, 0.01), 0.42, 0.58), pr) for r, (wr, pr) in rs.items()}
                for c, rs in base.items()}
        matchups = {}
        for r in ROLES:
            in_role = [c for c in ids if r in champ_roles[c]]
            for i, a in enumerate(in_role):
                for b in in_role[i + 1:]:
                    if rng.random() < 0.6:
                        matchups[(r, a, b)] = _clamp(rng.gauss(0.5, 0.03), 0.40, 0.60)
        synergy = {frozenset(rng.sample(ids, 2)): _clamp(rng.gauss(0.5, 0.02), 0.44, 0.56) for _ in range(400)}
        ban_rates = {c: rng.uniform(0.0, 0.35) if rng.random() < 0.3 else rng.uniform(0.0, 0.03) for c in ids}
        brackets[name] = BracketStats(meta, matchups, synergy, ban_rates)

    tiers = {}
    for r in ROLES:
        pool = rng.sample([c for c in ids if r in champ_roles[c]], 4)
        tiers[r] = dict(zip(pool, ("main", "comfortable", "comfortable", "learning")))
    names = dict(names) if names else {c: f"Champ{c}" for c in ids}
    return EngineData(brackets, names), OurTeam.from_tiers(tiers, TEAM_BRACKETS)


@dataclass(frozen=True)
class StepRecord:
    step: Step
    champ_id: int
    suggestions: list[Suggestion]  # empty on enemy turns
    seconds: float  # time to produce the suggestion set (0 on enemy turns)


def _enemy_action(engine: DraftEngine, state: DraftState) -> int:
    """Enemy policy: ban our highest-comfort available pool champion; pick their most likely champion."""
    unavailable = state.unavailable()
    if state.step.kind == "ban":
        pool = [(e.comfort, c) for r in ROLES for c, e in engine.team.pools.get(r, {}).items() if c not in unavailable]
        if pool:
            return max(pool, key=lambda t: (t[0], -t[1]))[1]
    probs = engine.enemy_pick_probs(state)
    if probs:
        return max(probs, key=lambda c: (probs[c], -c))
    return min(c for c in engine.data.champions() if c not in unavailable)


def run_draft(engine: DraftEngine, fmt: DraftFormat, our_side: Side, k: int = 3) -> tuple[DraftState, list[StepRecord]]:
    state = DraftState(fmt, our_side)
    records = []
    while not state.is_complete:
        step = state.step
        if state.is_our_turn:
            t0 = time.perf_counter()
            suggestions = engine.suggest(state, k)
            elapsed = time.perf_counter() - t0
            if not suggestions:
                raise RuntimeError(f"no suggestions at step {step.index + 1}")
            champ = suggestions[0].champ_id
        else:
            suggestions, elapsed, champ = [], 0.0, _enemy_action(engine, state)
        records.append(StepRecord(step, champ, suggestions, elapsed))
        state = state.apply(champ)
    return state, records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--format", default="tournament", choices=["tournament", "ranked"])
    parser.add_argument("--side", default="blue", choices=["blue", "red"])
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # reasons use U+2212; Windows consoles may not

    data, team = synthetic_data(args.seed)
    engine = DraftEngine(data, team)
    state, records = run_draft(engine, get_format(args.format), args.side)
    for rec in records:
        s = rec.step
        who = "us" if s.side == args.side else "them"
        print(f"{s.index + 1:>2}. {s.label:<6} {s.side:<4} ({who:<4}) {s.kind:<4} -> {data.name(rec.champ_id)}")
        for sug in rec.suggestions:
            role = f" [{ROLE_LABELS[sug.role]}]" if sug.kind == "pick" else ""
            print(f"      {sug.name:<10}{role:<10} {sug.value:+.3f}  {sug.reason}")
        if rec.suggestions:
            print(f"      ({rec.seconds * 1000:.0f} ms)")
    worst = max(r.seconds for r in records if r.suggestions)
    ev = engine.evaluate(state)
    print(f"\nUs:   {', '.join(f'{data.name(c)} ({ROLE_LABELS[r]})' for c, r in ev.our_roles.items())}")
    print(f"Them: {', '.join(f'{data.name(c)} ({ROLE_LABELS[r]})' for c, r in ev.their_roles.items())}")
    print(f"Model P(win) = {ev.win_prob:.1%}   slowest suggestion set: {worst * 1000:.0f} ms")


if __name__ == "__main__":
    main()
