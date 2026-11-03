"""Synergy v2 in Live scoring: the engine's synergy term is Stat + Style (pairs) and Team vs enemy, replacing
the v1 Stat-only term and the 85% damage penalty. See docs/synergy-v2.md ("Live scoring")."""
import random
import time
from dataclasses import replace

import pytest

from draftassist.api.live import build_live_view
from draftassist.data.provider import ChampionCatalog
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.model import ROLES
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import DraftState
from draftassist.engine.style import StyleModel, parse_style_rules
from draftassist.engine.synergy_v2 import SynergyEvaluator
from draftassist.engine.team import TeamModel, parse_team_rules
from draftassist.engine.traits import TAGS, ChampionTraits, TagEntry
from draftassist.lcu.parser import LiveDraft
from test_engine_synergy import (
    BANS, DAMAGE, GNAR, KAISA, NAUTILUS, SEJUANI, SYN, TEAM, VI, data, state,
)

STYLE = [{"pair": "ADC-SUP", "when": {"SUP": "engage", "ADC": "dive"}, "prior_pp": 2.0, "note": "All-in bot"}]
TEAM_RULES = [
    {"id": "has_engage", "feature": {"max": "engage"}, "prior_pp": 2.0, "note": "A reliable engage"},
    {"id": "damage_imbalance", "feature": {"special": "damage_imbalance"}, "prior_pp": -1.5, "note": "One type"},
    {"id": "engage_vs_disengage", "ours": {"max": "engage"}, "theirs": {"max": "disengage"}, "prior_pp": -1.0,
     "note": "Disengage"},
]


def tags(**v):
    return TagEntry({**dict.fromkeys(TAGS, 0), **v}, reviewed=True)


def evaluator(stat=SYN, damage=DAMAGE):
    t = {(KAISA, "adc"): tags(dive=3), (NAUTILUS, "support"): tags(engage=3), (SEJUANI, "jungle"): tags(engage=2),
         (VI, "jungle"): tags(disengage=3)}
    measured = {k: {"dmg_phys": d[0], "dmg_magic": d[1], "dmg_true": d[2], "games": 100} for k, d in damage.items()}
    traits = ChampionTraits(measured, t, {})
    return SynergyEvaluator(stat, traits, StyleModel(parse_style_rules(STYLE), {}),
                            TeamModel(parse_team_rules(TEAM_RULES), traits))


def engine(**kw):
    return DraftEngine(replace(data(), synergy_v2=evaluator()), TEAM, **kw)


def test_synergy_term_uses_stat_style_and_team():
    st = state(*BANS, KAISA, VI, GNAR, NAUTILUS, SEJUANI)
    eng = engine()
    ev = eng.evaluate(st)
    v2 = eng.data.synergy_v2
    ours = {r: c for c, r in ev.our_roles.items()}
    theirs = {r: c for c, r in ev.their_roles.items()}
    pairs_ours = sum(p.contribution for p in v2.evaluate(ours, full=False).pairs)
    pairs_theirs = sum(p.contribution for p in v2.evaluate(theirs, full=False).pairs)
    team = v2.weights.team * v2.team.team_vs([(c, r) for r, c in ours.items()], [(c, r) for r, c in theirs.items()],
                                             fill=True).total
    assert ev.terms.syn_us == pytest.approx(pairs_ours)
    assert ev.terms.syn_them == pytest.approx(pairs_theirs)
    assert ev.terms.team == pytest.approx(team) and ev.terms.team != 0
    assert ev.terms.damage == 0.0  # the 85% penalty is replaced by the team model's damage rule
    v1 = DraftEngine(data(), TEAM).evaluate(st)
    assert v1.terms.damage == pytest.approx(-0.05) and v1.terms.team == 0.0  # without v2: unchanged


def test_batched_pick_scores_match_single_evaluations():
    eng = engine()
    st = state(*BANS, KAISA, VI, GNAR)  # B2: our second pick
    for s in eng.suggest_picks(st, k=20):
        single = eng.evaluate(DraftState(TOURNAMENT, "blue", st.actions + (s.champ_id,)))
        # the champion's role in the single evaluation is the engine's best assignment; compare like with like
        if single.our_roles.get(s.champ_id) == s.role:
            assert s.win_prob == pytest.approx(single.win_prob, abs=1e-9)


def test_pick_reason_names_the_v2_pair_synergy():
    eng = engine()
    st = state(*BANS, KAISA, VI, GNAR)
    naut = next(s for s in eng.suggest_picks(st, k=20) if s.champ_id == NAUTILUS)
    assert "with your locked Kai'Sa" in naut.reason
    pair = eng.data.synergy_v2.pair("adc", KAISA, "support", NAUTILUS)
    assert f"{25 * pair.contribution:+.1f}".replace("-", "−") in naut.reason


def test_next_two_picks_report_v2_pair_synergy():
    eng = engine()
    st = state(*BANS, KAISA, VI, GNAR)
    pairs = eng.suggest_pick_pairs(st, k=3)
    assert pairs
    for p in pairs:
        roles = {p.first.role: p.first.champ_id, p.second.role: p.second.champ_id}
        (ra, a), (rb, b) = list(roles.items())
        assert p.synergy_pp == pytest.approx(25 * eng.data.synergy_v2.pair(ra, a, rb, b).contribution)


# ---------- budgets on full-size data ----------
def full_engine():
    full, team = synthetic_data()
    rng = random.Random(3)
    t = {}
    for c in full.champions():
        for r in ROLES:
            if full.brackets["gold"].role_stat(c, r):
                t[(c, r)] = tags(**{k: rng.randint(0, 3) for k in TAGS})
    measured = {(c, r): {"dmg_phys": d[0], "dmg_magic": d[1], "dmg_true": d[2], "frontline_all": rng.random(),
                         "games": 100} for (c, r), d in full.damage.items()}
    traits = ChampionTraits(measured, t, {})
    from draftassist.data.rules import load_style_rules, load_team_rules

    ev = SynergyEvaluator(full.synergy, traits, StyleModel(load_style_rules(), {}),
                          TeamModel(load_team_rules(), traits))
    ev.warm(team.pools)
    return DraftEngine(replace(full, synergy_v2=ev), team), full, team


def live(st):
    return LiveDraft(st, (), (), "BAN_PICK", 20.0, None, None, 1)


def test_live_budgets_with_synergy_v2_on_full_size_data():
    eng, full, team = full_engine()
    catalog = ChampionCatalog("16.21.1", {c: (str(c), str(c)) for c in full.names})
    rng = random.Random(5)
    pool = sorted(team.pool_champs())
    others = [c for c in sorted(full.champions()) if c not in team.pool_champs()]
    worst_pick = worst_ban = 0.0
    for _ in range(6):
        bans = rng.sample(others, 6)
        b1 = rng.choice(pool)
        r1, r2 = rng.sample([c for c in others if c not in bans], 2)
        st = DraftState(TOURNAMENT, "blue", (*bans, b1, r1, r2))  # B2/B3: picks + Next 2 picks
        t0 = time.perf_counter()
        view = build_live_view(eng, catalog, live(st), "fixture", None)
        worst_pick = max(worst_pick, time.perf_counter() - t0)
        assert view["center"]["next_two"]
        t0 = time.perf_counter()
        build_live_view(eng, catalog, live(DraftState(TOURNAMENT, "blue")), "fixture", None)
        worst_ban = max(worst_ban, time.perf_counter() - t0)
    assert worst_pick < 0.200 and worst_ban < 0.200
    flex = DraftState(STANDARD_RANKED, "blue")
    t0 = time.perf_counter()
    eng.ban_group(flex, list(ROLES))
    assert time.perf_counter() - t0 < 0.300
