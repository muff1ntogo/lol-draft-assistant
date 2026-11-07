"""Fuzz: 10,000 seeded random legal draft states across both formats. Every list the engine
produces must hold Rules 1-3. Uses 60-champion synthetic data to keep the run short; the rules
don't depend on champion count (the 200 ms budget is checked on full-size data elsewhere)."""
import random

import pytest

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.model import ROLES
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import SKIPPED, DraftState

N_STATES = 10_000


def random_state(rng: random.Random, champs: list[int]) -> DraftState:
    state = DraftState(rng.choice([TOURNAMENT, STANDARD_RANKED]), rng.choice(["blue", "red"]))
    for _ in range(rng.randint(0, 19)):
        if state.step.kind == "ban" and rng.random() < 0.15:
            state = state.apply(SKIPPED)
        else:
            state = state.apply(rng.choice([c for c in champs if c not in state.unavailable()]))
    open_champs = [c for c in champs if c not in state.unavailable()]
    hovers = tuple((c, rng.choice(ROLES)) for c in rng.sample(open_champs, rng.randint(0, 2)))
    return DraftState(state.format, state.our_side, state.actions, hovers)


@pytest.mark.parametrize("chunk", range(4))
def test_fuzz_suggestions_respect_rules(chunk):
    data, team = synthetic_data(seed=11, n_champs=60)
    engine = DraftEngine(data, team)
    champs = sorted(data.champions())
    min_rel = engine.cfg.min_ban_relevance
    rng = random.Random(1000 + chunk)
    for i in range(N_STATES // 4):
        state = random_state(rng, champs)
        if state.is_complete:
            continue
        unavailable = state.unavailable()
        lists = [engine.likely_enemy_picks(state)]
        if state.is_our_turn:
            lists.append(engine.suggest(state))
        if state.format.simultaneous_bans and state.step.kind == "ban":  # Flex group ban turn
            group = [s for _, s in engine.ban_group(state, list(ROLES)) if s]
            declared = {c for c, _ in state.hovers}
            assert not declared & {s.champ_id for s in group}, "banned a teammate's declared pick"
            lists.append(group)
        for sugs in lists:
            where = f"chunk {chunk} state {i}: {state.format.name} {state.our_side} {state.actions}"
            got = [s.champ_id for s in sugs]
            assert len(sugs) <= 5, where
            assert len(set(got)) == len(got), f"duplicate in {where}"
            assert not unavailable & set(got), f"unavailable suggested in {where}"
            for s in sugs:
                if s.kind == "ban":
                    assert s.relevance >= min_rel and s.value > 0, f"wasted ban {s} in {where}"
