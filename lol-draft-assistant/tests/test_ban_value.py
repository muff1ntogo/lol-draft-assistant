"""docs/ban-value-matchups.md: ban value = rel * discount * (a*Threat + b*Matchup + c*Comp)."""
import random

import pytest

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.model import BanWeights, BracketStats, EngineConfig, EngineData, OurTeam, RoleStat
from draftassist.engine.simulate import run_draft, synthetic_data
from draftassist.engine.state import SKIPPED, DraftState

P, P2, X1, X2, Y = 1, 2, 11, 12, 13
OTHERS = (21, 22, 23, 24)  # the remaining 82% of top pick rate, all even matchups


def top_data(extra_matchups=None, extra_meta=None):
    meta = {
        P: {"top": RoleStat(0.50, 0.0)},  # our answer; not an enemy option itself
        X1: {"top": RoleStat(0.52, 0.12)},
        X2: {"top": RoleStat(0.51, 0.06)},
        **{o: {"top": RoleStat(0.50, 0.205)} for o in OTHERS},
        **(extra_meta or {}),
    }
    matchups = {("top", P, X1): 0.54, ("top", P, X2): 0.42, **{("top", P, o): 0.50 for o in OTHERS},
                **(extra_matchups or {})}
    names = {c: f"C{c}" for c in meta}
    return EngineData({"gold": BracketStats(meta, matchups)}, names)


def team(top_pool):
    return OurTeam.from_tiers({"top": top_pool}, {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")})


EMPTY = DraftState(TOURNAMENT, "blue")


def rows_by_champ(engine, state=EMPTY):
    return {r["champ_id"]: r for r in engine.ban_breakdown(state)}


# 1. Matchup beats popularity ---------------------------------------------------------------
def test_matchup_beats_popularity():
    engine = DraftEngine(top_data(), team({P: "main"}))
    tables = engine.lane_ban_tables(EMPTY)
    assert tables["top"].row(X1).ban == pytest.approx(-0.545, abs=0.001)
    assert tables["top"].row(X2).ban == pytest.approx(0.511, abs=0.001)
    order = [r["champ_id"] for r in engine.ban_breakdown(EMPTY)]
    assert order.index(X2) < order.index(X1)
    assert rows_by_champ(engine)[X1]["Matchup"] < 0  # signed, not clipped


# 2. A champion our pool counters is discouraged ------------------------------------------
def test_countered_champion_is_not_suggested():
    data = top_data(extra_matchups={("top", P, Y): 0.55}, extra_meta={Y: {"top": RoleStat(0.50, 0.10)}})
    engine = DraftEngine(data, team({P: "main"}))
    row = rows_by_champ(engine)[Y]
    assert row["Matchup"] < 0 and row["value"] <= 0
    assert Y not in [s.champ_id for s in engine.suggest_bans(EMPTY, k=50)]


# 3. Slider extremes ---------------------------------------------------------------------------
def test_slider_extremes_rank_by_one_component():
    data, t = synthetic_data()
    for slider, component in ((0.0, "Threat"), (1.0, "Matchup")):
        engine = DraftEngine(data, t, EngineConfig(ban_weights=BanWeights.from_slider(slider, own=0.0)))
        rows = engine.ban_breakdown(EMPTY)
        by_value = [r["champ_id"] for r in rows]
        by_component = [r["champ_id"] for r in sorted(rows, key=lambda r: (-r[component], r["champ_id"]))]
        assert by_value == by_component, component
    assert BanWeights.from_slider(0.3).threat + BanWeights.from_slider(0.3).matchup == pytest.approx(2.5)


# 4. Eligibility ---------------------------------------------------------------------------------
def test_banned_pool_champion_is_no_longer_an_answer():
    data = top_data(extra_matchups={("top", P2, X2): 0.52}, extra_meta={P2: {"top": RoleStat(0.50, 0.0)}})
    engine = DraftEngine(data, team({P: "main", P2: "comfortable"}))
    before = rows_by_champ(engine)[X2]
    after_state = DraftState(TOURNAMENT, "blue", (SKIPPED, P2))  # the enemy bans our P2
    after = rows_by_champ(engine, after_state)[X2]
    assert engine.lane_ban_tables(after_state)["top"].row(X2).via == P
    assert after["Matchup"] > before["Matchup"] and after["value"] > before["value"]


# 5. Comp ------------------------------------------------------------------------------------
def test_comp_zero_before_our_first_pick_then_counters_count():
    data = top_data(extra_matchups={("top", X2, P): 0.58})
    engine = DraftEngine(data, team({P: "main"}))
    assert all(r["Comp"] == 0 for r in engine.ban_breakdown(EMPTY))
    picked = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6 + (P,))  # we locked P top
    rows = rows_by_champ(engine, picked)
    assert rows[X2]["Comp"] > 0  # X2 counters our locked P
    assert rows[OTHERS[0]]["Comp"] == pytest.approx(0)


# 6. Component sum ------------------------------------------------------------------------------
def test_components_times_weights_and_multipliers_equal_value():
    data, t = synthetic_data()
    engine = DraftEngine(data, t, EngineConfig(ban_weights=BanWeights(0.7, 1.9, 1.3, 0.8)))
    w = engine.cfg.ban_weights
    final, _ = run_draft(engine, TOURNAMENT, "blue")
    states = [DraftState(TOURNAMENT, "blue", final.actions[:i]) for i in (0, 9, 13)]
    states.append(DraftState(STANDARD_RANKED, "red", (SKIPPED,) * 2))  # blind Flex bans: discount < 1
    discounted = 0
    for st in states:
        for r in engine.ban_breakdown(st):
            expected = (r["rel"] * r["discount"] * (w.threat * r["Threat"] + w.matchup * r["Matchup"] + w.comp * r["Comp"])
                        - w.own * r["OwnLoss"])
            assert abs(expected - r["value"]) < 1e-9
            discounted += r["discount"] < 1
    assert discounted > 0


def test_suggestions_carry_their_components():
    data, t = synthetic_data()
    engine = DraftEngine(data, t)
    for s in engine.suggest_bans(EMPTY, k=3):
        assert None not in (s.threat, s.matchup, s.comp, s.relevance, s.discount)


def test_ban_value_speed():
    import time

    data, t = synthetic_data()
    engine = DraftEngine(data, t)
    rng = random.Random(3)
    worst = 0.0
    for _ in range(20):
        actions = tuple(rng.sample(sorted(data.champions()), 13))
        st = DraftState(TOURNAMENT, "blue", actions)
        t0 = time.perf_counter()
        engine.suggest_bans(st, k=3)
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.200


# Settings: weights persist and drive the live engine --------------------------------------------
def test_settings_api_changes_ban_weights(make_services, engine, session):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app
    from draftassist.db.models import Champion
    from conftest import REAL_NAMES

    for cid, name in REAL_NAMES.items():
        session.add(Champion(champ_id=cid, key=name.replace("'", "").replace(" ", ""), name=name, patch="16.21.1"))
    session.commit()
    svc = make_services(db=engine)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.get("/api/settings").json() == {
            "ban_weights": {"threat": 1.0, "matchup": 1.5, "comp": 1.0, "own": 1.0}, "slider": 0.6,
            "ban_declared_intents": False, "alpha": 1.0, "beta": 1.0, "comfort_games_cap": 10,
            "damage_balance": True, "damage_penalty": -0.05, "ban_comp_synergy": False,
            "comp_coherence": True, "comp_weight": 0.15}
        body = {"ban_weights": {"threat": 2.5, "matchup": 0.0, "comp": 1.0}, "ban_declared_intents": True}
        assert client.put("/api/settings", json=body).json()["slider"] == 0.0
        assert svc.engine.cfg.ban_weights == BanWeights(2.5, 0.0, 1.0) and svc.engine.cfg.ban_declared_intents
        bad = {"ban_weights": {"threat": -1, "matchup": 0, "comp": 0}}
        assert client.put("/api/settings", json=bad).status_code == 422
        prep = client.get("/api/prep/lane-bans").json()
        assert all("team_ban_value" in row for role in prep["roles"] for row in role["rows"])



# Self-ban penalty ---------------------------------------------------------------------------------
def test_banning_our_best_pick_costs_what_we_lose():
    """P2 is our top laner's best option, P the next: banning P2 costs exactly their gap."""
    data = top_data(extra_meta={P2: {"top": RoleStat(0.56, 0.08)}})  # strong, and the enemy plays it too
    engine = DraftEngine(data, team({P: "main", P2: "main"}))
    rows = rows_by_champ(engine)
    gap = engine._weight("us", P2, "top") - engine._weight("us", P, "top")
    assert gap > 0
    assert rows[P2]["OwnLoss"] == pytest.approx(gap)
    assert P not in rows  # never a candidate: the enemy doesn't play it
    assert rows[X2]["OwnLoss"] == 0  # not in our pool
    w = engine.cfg.ban_weights
    enemy_side = rows[P2]["rel"] * rows[P2]["discount"] * (w.threat * rows[P2]["Threat"] + w.matchup * rows[P2]["Matchup"])
    assert rows[P2]["value"] == pytest.approx(enemy_side - w.own * gap)
    assert P2 not in [s.champ_id for s in engine.suggest_bans(EMPTY, k=50)]  # costs us more than it denies

    lenient = DraftEngine(data, team({P: "main", P2: "main"}), EngineConfig(ban_weights=BanWeights(own=0.0)))
    assert rows_by_champ(lenient)[P2]["value"] == pytest.approx(enemy_side)  # weight 0: penalty off


def test_no_penalty_once_that_role_is_locked():
    data = top_data(extra_meta={P2: {"top": RoleStat(0.56, 0.08)}})
    engine = DraftEngine(data, team({P: "main", P2: "main"}))
    locked = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6 + (P,))  # our top laner already locked P
    assert rows_by_champ(engine, locked)[P2]["OwnLoss"] == 0


def test_own_pick_ban_carries_a_warning():
    data = top_data(extra_meta={P2: {"top": RoleStat(0.60, 0.30)}})  # a meta monster: still worth banning?
    engine = DraftEngine(data, team({P: "main", P2: "main"}), EngineConfig(ban_weights=BanWeights(own=0.05)))
    sugs = {s.champ_id: s for s in engine.suggest_bans(EMPTY, k=50)}
    assert P2 in sugs  # a low penalty weight still lets a strong enough own-pool ban through...
    assert "our top's best pick" in " ".join(sugs[P2].warnings)  # ...flagged on the card
    assert sugs[P2].own_loss > 0
