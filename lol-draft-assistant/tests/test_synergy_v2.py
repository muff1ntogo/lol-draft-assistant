"""Synergy v2, Stage 3: Stat / Style / Team components, Lane and Overall, and the evaluate API.
See docs/synergy-v2.md."""
import itertools
import random
import time

import numpy as np
import pytest

from draftassist.engine.style import StyleModel, parse_style_rules
from draftassist.engine.synergy import PairScore, SynergyTable
from draftassist.engine.synergy_v2 import (
    CORE_PAIRS,
    SynergyEvaluator,
    SynergyWeights,
    confidence,
    lane_score,
)
from draftassist.engine.team import TeamModel, parse_team_rules
from draftassist.engine.traits import TAGS, ChampionTraits, TagEntry

ROLES = ("top", "jungle", "mid", "adc", "support")


def tags(**v):
    return TagEntry({**dict.fromkeys(TAGS, 0), **v}, reviewed=True)


STYLE = [
    {"pair": "ADC-SUP", "when": {"SUP": "enchanter", "ADC": "hypercarry"}, "prior_pp": 2.0, "note": "Ench + carry"},
    {"pair": "JG-MID", "when": {"MID": "pick", "JG": "pick"}, "prior_pp": 1.0, "note": "Catch duo"},
    {"pair": "TOP-JG", "when": {"TOP": "engage", "JG": "engage"}, "prior_pp": 0.8, "note": "Layered engage"},
]
TEAM = [
    {"id": "has_engage", "feature": {"max": "engage"}, "prior_pp": 1.5, "note": "A reliable engage"},
    {"id": "no_frontline", "feature": {"lack": "frontline_all", "need": 1.5}, "prior_pp": -2.0, "note": "No front"},
    {"id": "engage_vs_disengage", "ours": {"max": "engage"}, "theirs": {"max": "disengage"}, "prior_pp": -1.0,
     "note": "Their disengage blunts our engage"},
]


def world(seed=0, per_role=8):
    """Champions 100*k + i in role k, random tags and measured traits, and a Stat table with random pairs."""
    rng = random.Random(seed)
    measured, t, pools = {}, {}, {}
    for k, role in enumerate(ROLES):
        pools[role] = [100 * (k + 1) + i for i in range(per_role)]
        for c in pools[role]:
            measured[(c, role)] = {"frontline_all": rng.random(), "games": 100,
                                   "dmg_phys": 0.5, "dmg_magic": 0.4, "dmg_true": 0.1}
            t[(c, role)] = tags(**{tag: rng.randint(0, 3) for tag in TAGS})
    traits = ChampionTraits(measured, t, {})
    pairs = {}
    for ra, rb in itertools.combinations(ROLES, 2):
        for a in pools[ra]:
            for b in pools[rb]:
                x, y = (ra, a), (rb, b)
                key = (x, y) if x < y else (y, x)
                games = rng.choice([0, 10, 150, 1500])
                s = rng.uniform(-0.2, 0.2) if games else 0.0
                pairs[key] = PairScore(s, s, games, games < 30)
    stat = SynergyTable("16.21", "all", pairs, {})
    style = StyleModel(parse_style_rules(STYLE), {})
    team_rules = parse_team_rules(TEAM)
    team = TeamModel(team_rules, traits)
    comps = [[(rng.choice(pools[r]), r) for r in ROLES] for _ in range(300)]
    team = team.with_means(team.features(comps).mean(axis=0))
    return SynergyEvaluator(stat, traits, style, team), pools


def test_lane_formula_at_full_and_zero_confidence():
    assert lane_score(0.12, 0.06, c=1.0) == pytest.approx((2 / 3) * 0.12 + (1 / 3) * 0.06)
    assert lane_score(0.12, 0.06, c=0.0) == pytest.approx(0.06)
    assert confidence(200) == pytest.approx(0.5)
    assert confidence(0) == 0.0


def test_pair_components():
    ev, pools = world()
    a, b = pools["adc"][0], pools["support"][0]
    p = ev.pair("adc", a, "support", b)
    assert p.core and p.games == ev.stat.pair("adc", a, "support", b).games
    assert p.stat == pytest.approx(ev.stat.pair("adc", a, "support", b).s)
    assert p.style == pytest.approx(ev.style.style(ev.traits, {"adc": a, "support": b}))
    assert p.lane == pytest.approx(lane_score(p.stat, p.style, confidence(p.games)))
    assert len(p.rules) <= 3
    other = ev.pair("top", pools["top"][0], "adc", a)
    assert not other.core and other.style == 0.0 and other.lane is None


def test_overall_decomposition_holds():
    ev, pools = world(seed=1)
    rng = random.Random(2)
    w = ev.weights
    for _ in range(50):
        comp = {r: rng.choice(pools[r]) for r in ROLES}
        res = ev.evaluate(comp)
        core = [p for p in res.pairs if p.core]
        other = [p for p in res.pairs if not p.core]
        assert len(core) == 3 and len(other) == 7
        direct = sum(w.stat * p.confidence * p.stat + (w.style + w.stat * (1 - p.confidence)) * p.style for p in core)
        direct += sum(w.stat * p.stat for p in other) + w.team * res.team
        assert res.overall == pytest.approx(direct, abs=1e-9)
        lanes = 0.75 * sum(p.lane for p in core) + 0.50 * sum(p.stat for p in other) + 0.25 * res.team
        assert res.overall == pytest.approx(lanes, abs=1e-9)
        assert res.stat_total + res.style_total + res.team_weighted == pytest.approx(res.overall, abs=1e-9)


def test_partial_comp_only_counts_present_pairs():
    ev, pools = world(seed=3)
    comp = {"jungle": pools["jungle"][0], "mid": pools["mid"][0]}
    res = ev.evaluate(comp)
    assert [(p.role_a, p.role_b) for p in res.pairs] == [("jungle", "mid")]
    assert res.stat_total + res.style_total + res.team_weighted == pytest.approx(res.overall, abs=1e-9)
    assert res.show_team is False  # Team is shown from 3 locked roles
    three = ev.evaluate(comp | {"top": pools["top"][0]})
    assert three.show_team is True


def test_team_vs_enemy_includes_cross_terms():
    ev, pools = world(seed=4)
    ours = {r: pools[r][0] for r in ROLES}
    enemy = [(pools[r][1], r) for r in ROLES]
    res = ev.evaluate(ours, enemy)
    expected = ev.team.team_vs(list((c, r) for r, c in ours.items()), enemy, fill=True)
    assert res.team_vs.total == pytest.approx(expected.total)
    assert any(rule.cross for rule, _ in res.team_vs.contributions)


def test_weights_from_config():
    ev, pools = world(seed=5)
    comp = {r: pools[r][0] for r in ROLES}
    heavy = SynergyEvaluator(ev.stat, ev.traits, ev.style, ev.team, SynergyWeights(stat=0.5, style=0.25, team=1.0))
    a, b = ev.evaluate(comp), heavy.evaluate(comp)
    assert b.overall - a.overall == pytest.approx(0.75 * a.team, abs=1e-9)
    with pytest.raises(ValueError):
        SynergyWeights(stat=-1)


def test_suggestions_exclude_banned_and_picked_champions():
    ev, pools = world(seed=6)
    comp = {"jungle": pools["jungle"][0], "mid": pools["mid"][0]}
    banned = {pools["top"][0], pools["adc"][1]}
    enemy = [(pools["support"][2], "support")]
    out = ev.suggest(comp, pools, unavailable=banned | {c for c, _ in enemy}, enemy=enemy, k=5)
    assert set(out) == {"top", "adc", "support"}
    for role, rows in out.items():
        assert len(rows) == 5
        ids = [r.champ for r in rows]
        assert len(set(ids)) == 5
        assert not set(ids) & banned
        assert pools["support"][2] not in ids
        gains = [r.gain for r in rows]
        assert gains == sorted(gains, reverse=True)
        best = rows[0]
        assert best.gain == pytest.approx(ev.evaluate(comp | {role: best.champ}, enemy).overall
                                          - ev.evaluate(comp, enemy).overall, abs=1e-9)


def test_evaluate_five_champions_under_100ms():
    ev, pools = world(seed=7, per_role=15)
    ev.warm(pools)
    comp = {r: pools[r][3] for r in ROLES}
    enemy = [(pools[r][4], r) for r in ROLES]
    ev.evaluate(comp, enemy)
    start = time.perf_counter()
    for _ in range(10):
        ev.evaluate(comp, enemy)
    assert (time.perf_counter() - start) / 10 < 0.1


# ---------- the API ----------
def test_evaluate_api(make_services, engine):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    svc = make_services(db=engine)
    ev, _ = world(seed=8)
    team_pools = svc.engine.team.pools
    svc.synergy_v2_override = ev
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        jg, mid = sorted(team_pools["jungle"])[0], sorted(team_pools["mid"])[0]
        banned = sorted(team_pools["top"])[0]
        start = time.perf_counter()
        body = c.post("/api/synergy/evaluate", json={
            "champions": [{"champ_id": jg, "role": "jungle"}, {"champ_id": mid, "role": "mid"}],
            "banned": [banned]}).json()
        assert time.perf_counter() - start < 0.5
        assert [p["roles"] for p in body["pairs"]] == [["jungle", "mid"]]
        p = body["pairs"][0]
        assert {"stat_pp", "style_pp", "lane_pp", "games", "confidence", "rules", "core"} <= set(p)
        team = body["team"]
        for key in ("stat_total_pp", "style_total_pp", "team_pp", "team_weighted_pp", "overall_pp", "identity",
                    "damage", "bars", "show_team"):
            assert key in team
        assert team["stat_total_pp"] + team["style_total_pp"] + team["team_weighted_pp"] == pytest.approx(
            team["overall_pp"], abs=0.02)
        assert set(body["suggestions"]) == {"top", "adc", "support"}
        assert banned not in [s["champ"]["id"] for s in body["suggestions"]["top"]]
        bad = c.post("/api/synergy/evaluate", json={"champions": [{"champ_id": jg, "role": "wizard"}]})
        assert bad.status_code == 422
        dup = c.post("/api/synergy/evaluate", json={"champions": [{"champ_id": jg, "role": "jungle"},
                                                                  {"champ_id": jg, "role": "mid"}]})
        assert dup.status_code == 422


def test_synergy_settings_api(make_services, engine):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    svc = make_services(db=engine)
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        assert c.get("/api/settings").json()["synergy_v2"]["w_stat"] == 0.5
        body = c.put("/api/settings/synergy", json={"values": {"w_team": 0.5, "conf_k": 100}}).json()
        assert body["synergy_v2"]["w_team"] == 0.5 and body["synergy_v2"]["conf_k"] == 100
        assert c.put("/api/settings/synergy", json={"values": {"w_stat": 0, "w_style": 0}}).status_code == 422
        assert c.put("/api/settings/synergy", json={"values": {"wombo": 1}}).status_code == 422
        assert svc.synergy_v2().weights.team == 0.5
