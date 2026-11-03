"""Synergy v2, Stage 2: pair style rules fitted to data (ridge centred on the priors), and the team model
(logistic, ridge priors, cross-team rules, comp identity). See docs/synergy-v2.md."""
import math

import numpy as np
import pytest

from draftassist.data.rules import STYLE_RULES_FILE, TEAM_RULES_FILE, load_style_rules, load_team_rules
from draftassist.engine.style import PAIRS, PairObs, StyleModel, fit_style, parse_style_rules
from draftassist.engine.synergy import pair_score
from draftassist.engine.team import (
    IDENTITIES,
    TeamModel,
    comp_identity,
    fit_team,
    parse_team_rules,
)
from draftassist.engine.traits import TAGS, ChampionTraits, TagEntry


def tags(**v):
    return TagEntry({**dict.fromkeys(TAGS, 0), **v}, reviewed=True)


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


# ---------- 2a: pair style rules ----------
STYLE_YAML = [
    {"pair": "ADC-SUP", "when": {"SUP": "enchanter", "ADC": "hypercarry"}, "prior_pp": 2.0,
     "note": "Enchanters enable hypercarries"},
    {"pair": "ADC-SUP", "when": {"SUP": "engage", "ADC": "not ranged_long"}, "prior_pp": 1.5,
     "note": "Engage support suits short-range all-in ADCs"},
    {"pair": "JG-MID", "when": {"MID": "lane_pressure", "JG": "dive"}, "prior_pp": 1.2, "note": "Mid prio"},
]


def bot_world(seed=0, n=20):
    """n ADCs with random hypercarry and range, n supports with random enchanter and engage, plus an unseen
    duo (900, 901) with hypercarry 3 and enchanter 3."""
    rng = np.random.default_rng(seed)
    t, ranges = {}, {}
    for i in range(n):
        t[(100 + i, "adc")] = tags(hypercarry=int(rng.integers(0, 4)))
        ranges[100 + i] = float(rng.choice([500, 550, 650]))
        t[(200 + i, "support")] = tags(enchanter=int(rng.integers(0, 4)), engage=int(rng.integers(0, 4)))
    t[(900, "adc")], ranges[900] = tags(hypercarry=3), 650.0
    t[(901, "support")] = tags(enchanter=3)
    return ChampionTraits({}, t, ranges), n


def test_parse_style_rules_and_strength():
    rules = parse_style_rules(STYLE_YAML)
    assert [r.pair for r in rules] == ["ADC-SUP", "ADC-SUP", "JG-MID"]
    assert rules[0].id == "ADC-SUP:SUP=enchanter,ADC=hypercarry"
    traits = ChampionTraits({}, {(1, "adc"): tags(hypercarry=3), (2, "support"): tags(enchanter=2)}, {1: 650.0})
    assert rules[0].strength(traits, {"adc": 1, "support": 2}) == pytest.approx(1.0 * 2 / 3)
    assert rules[1].strength(traits, {"adc": 1, "support": 2}) == 0.0  # engage 0; range 650 -> not ranged_long 0
    with pytest.raises(ValueError):
        parse_style_rules([{"pair": "ADC-SUP", "when": {"SUP": "wombo", "ADC": "hypercarry"}, "prior_pp": 1}])
    with pytest.raises(ValueError):
        parse_style_rules([{"pair": "TOP-SUP", "when": {"SUP": "engage", "TOP": "dive"}, "prior_pp": 1}])
    with pytest.raises(ValueError):
        parse_style_rules(STYLE_YAML + [STYLE_YAML[0]])  # duplicate rule


def test_unknown_trait_means_the_rule_does_not_fire():
    rules = parse_style_rules(STYLE_YAML)
    traits = ChampionTraits({(1, "jungle"): {"cc": 0.5}}, {(1, "jungle"): tags(dive=3)}, {})
    # mid 2 has no lane_pressure data at all: the rule is ignored, not counted as 0 or as 1
    assert rules[2].strength(traits, {"jungle": 1, "mid": 2}) == 0.0


def test_planted_interaction_is_recovered_and_predicted_for_an_unseen_pair():
    traits, n = bot_world()
    rules = parse_style_rules(STYLE_YAML)
    rng = np.random.default_rng(1)
    obs = []
    for i in range(n):
        for j in range(n):
            adc, sup = 100 + i, 200 + j
            strength = rules[0].strength(traits, {"adc": adc, "support": sup})
            s_true = 3.0 / 25 * strength  # planted +3pp at full strength
            games = 400
            wins = rng.binomial(games, sigmoid(s_true))
            obs.append(PairObs("ADC-SUP", {"adc": adc, "support": sup}, pair_score(wins, games, 0.5, 0.5).s_raw, games))
    fits = fit_style(rules, obs, traits)
    planted = fits[rules[0].id]
    assert 25 * planted.fitted == pytest.approx(3.0, abs=0.5)
    assert planted.support_games > 0
    model = StyleModel(rules, {r.id: fits[r.id].fitted for r in rules})
    unseen = model.style(traits, {"adc": 900, "support": 901})
    assert 25 * unseen == pytest.approx(3.0, abs=0.5)
    top = model.explain(traits, {"adc": 900, "support": 901})
    assert top[0][0].id == rules[0].id and 25 * top[0][1] == pytest.approx(25 * unseen, abs=1e-9)


def test_zero_data_gives_the_priors():
    traits, _ = bot_world()
    rules = parse_style_rules(STYLE_YAML)
    fits = fit_style(rules, [], traits)
    for r in rules:
        assert 25 * fits[r.id].fitted == pytest.approx(r.prior_pp)
        assert fits[r.id].support_games == 0


def test_little_data_stays_close_to_the_prior():
    traits, n = bot_world()
    rules = parse_style_rules(STYLE_YAML)
    rng = np.random.default_rng(2)
    obs = [PairObs("ADC-SUP", {"adc": 100 + i, "support": 200 + i},
                   pair_score(rng.binomial(10, 0.5), 10, 0.5, 0.5).s_raw, 10) for i in range(n)]
    fits = fit_style(rules, obs, traits)
    assert 25 * fits[rules[0].id].fitted == pytest.approx(2.0, abs=0.3)


def test_explain_lists_the_top_three_rules_by_size():
    traits = ChampionTraits({(1, "adc"): {}, (2, "support"): {}},
                            {(1, "adc"): tags(hypercarry=3), (2, "support"): tags(enchanter=3, engage=3)}, {1: 500.0})
    rules = parse_style_rules(STYLE_YAML)
    model = StyleModel(rules, {r.id: r.prior_pp / 25 for r in rules})
    top = model.explain(traits, {"adc": 1, "support": 2})
    assert [r.id for r, _ in top] == [rules[0].id, rules[1].id]  # the JG-MID rule doesn't apply to a bot duo
    assert 25 * top[0][1] == pytest.approx(2.0) and 25 * top[1][1] == pytest.approx(1.5)
    assert model.label(top[0]) == "Enchanters enable hypercarries +2.0"


def test_shipped_style_rules_are_valid():
    rules = load_style_rules(STYLE_RULES_FILE)
    for pair in PAIRS:
        assert 12 <= sum(r.pair == pair for r in rules) <= 18, pair
    assert all(r.note for r in rules)
    assert all(abs(r.prior_pp) <= 3 for r in rules)


# ---------- 2b: team model ----------
TEAM_YAML = [
    {"id": "no_frontline", "feature": {"lack": "frontline", "need": 1}, "prior_pp": -1.0, "note": "No frontline"},
    {"id": "has_engage", "feature": {"max": "engage"}, "prior_pp": 1.5, "note": "Reliable engage"},
    {"id": "engage_vs_disengage", "ours": {"max": "engage"}, "theirs": {"max": "disengage"}, "prior_pp": -1.0,
     "note": "Disengage blunts engage"},
]


def team_world(seed=0, per_role=12):
    """Champions per role: most have no frontline, engage or disengage, a few have a lot, so comps differ."""
    rng = np.random.default_rng(seed)
    measured, t = {}, {}
    pools = {}
    for k, role in enumerate(("top", "jungle", "mid", "adc", "support")):
        pools[role] = []
        for i in range(per_role):
            c = 1000 * (k + 1) + i
            pools[role].append(c)
            measured[(c, role)] = {"frontline": float(rng.choice([0.0, 0.0, 0.0, 0.1, 0.9])), "games": 100}
            t[(c, role)] = tags(engage=int(rng.choice([0, 0, 0, 3])), disengage=int(rng.choice([0, 0, 0, 3])))
    return ChampionTraits(measured, t, {}), pools


def random_comps(pools, n, rng):
    roles = list(pools)
    picks = {r: rng.choice(np.array(pools[r]), size=n).tolist() for r in roles}
    return [[(picks[r][i], r) for r in roles] for i in range(n)]


def test_parse_team_rules():
    rules = parse_team_rules(TEAM_YAML)
    assert [r.id for r in rules] == ["no_frontline", "has_engage", "engage_vs_disengage"]
    assert rules[2].cross and not rules[0].cross
    with pytest.raises(ValueError):
        parse_team_rules([{"id": "x", "feature": {"max": "wombo"}, "prior_pp": 1}])
    with pytest.raises(ValueError):
        parse_team_rules([{"id": "x", "feature": {"median": "engage"}, "prior_pp": 1}])


def test_team_features():
    traits = ChampionTraits(
        {(1, "top"): {"frontline": 0.9}, (2, "jungle"): {"frontline": 0.3}, (3, "mid"): {"frontline": 0.0},
         (4, "adc"): {"frontline": 0.0}, (5, "support"): {"frontline": 0.0}},
        {(c, r): tags(engage=e) for c, r, e in ((1, "top", 0), (2, "jungle", 3), (3, "mid", 0), (4, "adc", 0),
                                                 (5, "support", 2))}, {})
    model = TeamModel(parse_team_rules(TEAM_YAML), traits)
    comp = [(1, "top"), (2, "jungle"), (3, "mid"), (4, "adc"), (5, "support")]
    f = model.features([comp])[0]
    assert f[0] == pytest.approx(0.0)  # frontline 0.9 + 0.3 >= 1: no shortfall
    assert f[1] == pytest.approx(1.0)  # max engage 3/3
    lacking = [(3, "mid"), (4, "adc"), (5, "support")]
    assert model.features([lacking])[0][0] == pytest.approx(1.0)  # nothing in front among the champions given
    # missing roles can be filled with an average champion of the role (partial comps, Stage 3)
    assert model.features([lacking], fill=True)[0][0] == pytest.approx(0.0)  # role averages 0.9 + 0.3


def test_excess_feature_counts_what_is_over_a_threshold():
    traits = ChampionTraits({}, {(1, "mid"): tags(hypercarry=3), (2, "adc"): tags(hypercarry=3),
                                 (3, "top"): tags(hypercarry=0)}, {})
    rules = parse_team_rules([{"id": "carries", "feature": {"excess": "hypercarry", "over": 1, "cap": 1},
                               "prior_pp": -0.5}])
    model = TeamModel(rules, traits)
    assert model.features([[(1, "mid"), (2, "adc"), (3, "top")]])[0][0] == pytest.approx(1.0)  # 2 carries: 1 over
    assert model.features([[(2, "adc"), (3, "top")]])[0][0] == pytest.approx(0.0)


def test_planted_no_frontline_penalty_is_recovered():
    traits, pools = team_world()
    rules = parse_team_rules(TEAM_YAML)
    model = TeamModel(rules, traits)
    rng = np.random.default_rng(3)
    # 100k matches: the prior (-1pp, SD 1pp) is strong, so a 3pp disagreement needs a big sample to move it
    blue, red = random_comps(pools, 100_000, rng), random_comps(pools, 100_000, rng)
    fb, fr = model.features(blue), model.features(red)
    logit = -4.0 / 25 * (fb[:, 0] - fr[:, 0])  # planted: no frontline costs 4pp; nothing else matters
    y = rng.random(len(blue)) < 1 / (1 + np.exp(-logit))
    fit = fit_team(model, blue, red, y)
    assert 25 * fit.coefs["no_frontline"] == pytest.approx(-4.0, abs=0.8)
    assert abs(25 * fit.coefs["has_engage"]) < 1.0


def test_cross_term_engage_vs_disengage_is_negative():
    traits, pools = team_world(seed=4)
    rules = parse_team_rules(TEAM_YAML)
    model = TeamModel(rules, traits)
    assert fit_team(model, [], [], np.array([], dtype=bool)).coefs["engage_vs_disengage"] < 0  # prior alone
    rng = np.random.default_rng(5)
    blue, red = random_comps(pools, 30_000, rng), random_comps(pools, 30_000, rng)
    cb, cr = model.cross(blue, red), model.cross(red, blue)
    logit = -3.0 / 25 * (cb[:, 0] - cr[:, 0])
    y = rng.random(len(blue)) < 1 / (1 + np.exp(-logit))
    fit = fit_team(model, blue, red, y)
    assert 25 * fit.coefs["engage_vs_disengage"] == pytest.approx(-3.0, abs=1.0)


def test_zero_team_data_gives_the_priors():
    traits, _ = team_world()
    model = TeamModel(parse_team_rules(TEAM_YAML), traits)
    fit = fit_team(model, [], [], np.array([], dtype=bool))
    assert {k: round(25 * v, 9) for k, v in fit.coefs.items()} == {"no_frontline": -1.0, "has_engage": 1.5,
                                                                   "engage_vs_disengage": -1.0}


def test_team_score_is_relative_to_the_average_comp_and_vs_enemy_adds_cross_terms():
    traits, pools = team_world(seed=6)
    rules = parse_team_rules(TEAM_YAML)
    model = TeamModel(rules, traits, coefs={r.id: r.prior_pp / 25 for r in rules})
    rng = np.random.default_rng(7)
    comps = random_comps(pools, 2_000, rng)
    model = model.with_means(model.features(comps).mean(axis=0))
    scores = [model.team(c) for c in comps]
    assert abs(np.mean(scores)) < 0.01  # the average comp scores ~0
    ours, theirs = comps[0], comps[1]
    vs = model.team_vs(ours, theirs)
    f_ours, f_theirs = model.features([ours])[0], model.features([theirs])[0]
    cross = model.cross([ours], [theirs])[0] - model.cross([theirs], [ours])[0]
    expected = (f_ours - f_theirs) @ np.array([-1.0, 1.5]) / 25 + cross[0] * -1.0 / 25
    assert vs.total == pytest.approx(expected)


def test_comp_identity():
    t = {(1, "top"): tags(engage=3), (2, "jungle"): tags(engage=3, dive=2), (3, "mid"): tags(engage=2),
         (4, "adc"): tags(hypercarry=1), (5, "support"): tags(engage=3)}
    traits = ChampionTraits({}, t, {})
    ident = comp_identity(traits, list((c, r) for (c, r) in t))
    assert ident.label == "engage" and ident.coherence > 0.7
    assert set(ident.scores) == set(IDENTITIES)
    poke = {(1, "top"): tags(poke=3), (2, "jungle"): tags(poke=2), (3, "mid"): tags(poke=3),
            (4, "adc"): tags(poke=3), (5, "support"): tags(enchanter=3)}
    mixed = comp_identity(ChampionTraits({}, poke, {}), list(poke))
    assert mixed.label == "poke" and mixed.coherence < 0.9


def test_damage_imbalance_replaces_the_85_percent_penalty():
    rules = parse_team_rules([{"id": "damage_imbalance", "feature": {"special": "damage_imbalance"},
                               "prior_pp": -1.5, "note": "One damage type"}])
    ad = {(c, r): {"dmg_phys": 0.95, "dmg_magic": 0.05, "dmg_true": 0.0}
          for c, r in ((1, "top"), (2, "jungle"), (3, "mid"), (4, "adc"), (5, "support"))}
    mixed = dict(ad) | {(3, "mid"): {"dmg_phys": 0.05, "dmg_magic": 0.95, "dmg_true": 0.0},
                        (2, "jungle"): {"dmg_phys": 0.2, "dmg_magic": 0.8, "dmg_true": 0.0}}
    comp = list(ad)
    assert TeamModel(rules, ChampionTraits(ad, {}, {})).features([comp])[0][0] > 0.5
    assert TeamModel(rules, ChampionTraits(mixed, {}, {})).features([comp])[0][0] == 0.0
    prof = TeamModel(rules, ChampionTraits(mixed, {}, {})).damage_profile(comp)
    assert sum(prof.values()) == pytest.approx(1.0)


def test_shipped_team_rules_are_valid():
    rules = load_team_rules(TEAM_RULES_FILE)
    own = [r for r in rules if not r.cross]
    cross = [r for r in rules if r.cross]
    assert len(own) >= 12 and len(cross) >= 4
    ids = {r.id for r in rules}
    for needed in ("has_engage", "no_frontline", "peel_for_carry", "damage_imbalance", "curve_coherence",
                   "identity_coherence", "engage_vs_disengage", "poke_vs_engage", "pick_vs_immobile_carry"):
        assert needed in ids


# ---------- fits stored by aggregation, the model loaded back, the Rules page ----------
from sqlmodel import Session, select  # noqa: E402

from draftassist.data.aggregate import aggregate  # noqa: E402
from draftassist.data.rules import dump_style_rules, dump_team_rules  # noqa: E402
from draftassist.data.synergy_model import fit_synergy_model, load_synergy_model  # noqa: E402
from draftassist.db.models import Champion, SynergyFit  # noqa: E402


@pytest.fixture
def rule_files(tmp_path):
    style, team = tmp_path / "style_rules.yaml", tmp_path / "team_rules.yaml"
    dump_style_rules(parse_style_rules(STYLE_YAML), style)
    dump_team_rules(parse_team_rules(TEAM_YAML), team)
    return style, team


@pytest.fixture
def crawled(engine):
    from test_traits import BLUE, RED, match, store

    with Session(engine) as s:
        for c in BLUE + RED:
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1", attack_range=550))
        s.commit()
    for i in range(40):
        store(engine, match(f"EUW1_{i}", blue_win=i % 3 != 0))
    return engine


def test_aggregation_stores_fits_and_the_model_loads_back(crawled, rule_files):
    style, team = rule_files
    aggregate(crawled, "16.21")  # aggregation fits with the shipped files; refit with the test's files
    result = fit_synergy_model(crawled, "16.21", style, team)
    assert result["matches"] == 40 and result["pairs"] > 0
    with Session(crawled) as s:
        rows = s.exec(select(SynergyFit).where(SynergyFit.window == "16.21")).all()
    kinds = {r.model for r in rows}
    assert {"style", "team", "team_mean", "team_side"} <= kinds
    model = load_synergy_model(crawled, style_path=style, team_path=team)
    assert set(model.style.coefs) == {r.id for r in parse_style_rules(STYLE_YAML)}
    assert set(model.team.coefs) == {r["id"] for r in TEAM_YAML}
    assert model.team.means.shape == (2,)  # one mean per own-team rule
    # untagged champions with no style data: the rules don't fire, Style is 0
    assert model.style.style(model.traits, {"adc": 4, "support": 5}) == 0.0
    comp = [(1, "top"), (2, "jungle"), (3, "mid"), (4, "adc"), (5, "support")]
    assert isinstance(model.team.team(comp), float)


def test_fit_without_rule_files_or_data_is_harmless(engine, tmp_path):
    result = fit_synergy_model(engine, "16.21", tmp_path / "none.yaml", tmp_path / "none2.yaml")
    assert result == {"window": "16.21", "pairs": 0, "matches": 0, "style_rules": 0, "team_rules": 0}
    model = load_synergy_model(engine, style_path=tmp_path / "none.yaml", team_path=tmp_path / "none2.yaml")
    assert model.style.style(model.traits, {"adc": 1, "support": 2}) == 0.0


def test_rules_page_lists_rules_and_a_prior_edit_round_trips_to_yaml(make_services, crawled, rule_files):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app
    from draftassist.data.rules import load_style_rules, load_team_rules

    style, team = rule_files
    aggregate(crawled, "16.21")
    fit_synergy_model(crawled, "16.21", style, team)
    svc = make_services(db=crawled)
    svc.style_rules_path, svc.team_rules_path = style, team
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        body = c.get("/api/rules").json()
        assert body["window"] == "16.21" and body["prior_sd_pp"] == 1.0
        rule = next(r for r in body["style"] if r["id"] == "ADC-SUP:SUP=enchanter,ADC=hypercarry")
        assert rule["prior_pp"] == 2.0 and rule["fitted_pp"] is not None and "flagged" in rule
        assert rule["when"] == {"SUP": "enchanter", "ADC": "hypercarry"} and rule["note"]
        assert {r["id"] for r in body["team"]} == {"no_frontline", "has_engage", "engage_vs_disengage"}
        assert next(r for r in body["team"] if r["id"] == "engage_vs_disengage")["cross"] is True

        put = c.put("/api/rules", json={"kind": "style", "id": rule["id"], "prior_pp": 3.5})
        assert put.status_code == 200 and put.json()["prior_pp"] == 3.5
        assert next(r for r in load_style_rules(style) if r.id == rule["id"]).prior_pp == 3.5
        put = c.put("/api/rules", json={"kind": "team", "id": "has_engage", "prior_pp": -0.5})
        assert put.json()["prior_pp"] == -0.5
        assert next(r for r in load_team_rules(team) if r.id == "has_engage").prior_pp == -0.5
        # the refit uses the new prior
        assert next(r for r in c.get("/api/rules").json()["style"] if r["id"] == rule["id"])["prior_pp"] == 3.5
        assert c.put("/api/rules", json={"kind": "style", "id": "nope", "prior_pp": 1}).status_code == 422
        assert c.put("/api/rules", json={"kind": "team", "id": "has_engage", "prior_pp": 50}).status_code == 422


def test_flag_when_the_fit_disagrees_with_the_prior():
    from draftassist.engine.style import flagged

    assert not flagged(2.0, 1.0)
    assert flagged(2.0, 0.4)  # 1.6pp away
    assert flagged(0.5, -0.2)  # other sign
    assert not flagged(0.0, 0.3)
