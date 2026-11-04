"""Stage 3 of docs/matchup-archetypes.md: matchups shrink towards what archetypes, kits and strength predict."""
from __future__ import annotations

import random
import time

import numpy as np
import pytest
from sqlmodel import Session, select

from draftassist.data.aggregate import shrink
from draftassist.data.archetypes import (
    ARCHETYPES,
    Archetype,
    load_archetype_priors,
    priors_for,
)
from draftassist.data.matchup_model import format_matchup_report, load_matchup_model
from draftassist.data.provider import Bootstrap, bracket_stats, load_engine_inputs
from draftassist.db.models import Champion, CrawledMatch, MatchParticipant, Matchup
from draftassist.engine.engine import DraftEngine
from draftassist.engine.matchup_model import (
    RoleFeatures,
    empirical_k,
    fit_role,
    interactions_for,
    param_names,
    smooth,
)
from draftassist.engine.mathx import logit, sigmoid
from draftassist.engine.model import BracketStats, EngineData, OurTeam, RoleStat
from draftassist.engine.traits import KitEntry, KitTraits

TOP = ARCHETYPES["top"]


def arch(primary, secondary=None, reviewed=True):
    return Archetype(primary, secondary, reviewed)


def features(labels, kit=None, role="top"):
    champs = sorted(labels)
    return RoleFeatures(role, ARCHETYPES[role], champs, labels, KitTraits(kit or {}))


def simulate(feats, planted, n_games, seed=1, noise_sd=0.0):
    """Every pair plays n_games with a true log-odds of `planted` (param -> log-odds) plus pair noise."""
    rng = random.Random(seed)
    names = param_names(feats.words, feats.interactions)
    coefs = np.array([planted.get(p, 0.0) for p in names])
    idx = np.arange(len(feats.champs))
    a, b = np.triu_indices(len(idx), 1)
    true = feats.pair_features(a, b) @ coefs
    n = np.full(len(a), float(n_games))
    wins = np.array([sum(rng.random() < sigmoid(t + rng.gauss(0, noise_sd)) for _ in range(n_games)) for t in true],
                    dtype=float)
    return a, b, n, wins


# ---------- test 1: reduces to the old formula ----------
def test_with_no_effects_it_equals_the_old_shrinkage():
    for wins, games in ((0, 0), (3, 3), (40, 100), (2600, 5000)):
        assert smooth(wins, games, 200, sigmoid(0.0)) == pytest.approx(shrink(wins, games, 200))


# ---------- test 2: few games stay on the prior, many games leave it ----------
def test_few_games_stay_near_the_prior_and_many_games_barely_move():
    prior = sigmoid(0.2)
    assert abs(smooth(2, 3, 200, prior) - prior) < 0.005  # within 0.5pp
    assert abs(smooth(3200, 5000, 200, prior) - 0.64) < 0.005  # the data, not the prior
    assert smooth(0, 0, 50, prior) == pytest.approx(prior)


# ---------- test 3: planted effects are recovered; E is antisymmetric ----------
def test_recovers_a_planted_archetype_effect():
    labels = {i: arch(TOP[i % 5]) for i in range(30)}
    feats = features(labels)
    planted = {"E:juggernaut>ranged": -3 / 25}  # ranged beats juggernaut by 3pp
    a, b, n, wins = simulate(feats, planted, 400)
    coefs = fit_role(feats, a, b, n, wins, np.zeros(30), np.zeros(len(feats.names)), prior_sd_pp=1.5)
    got = dict(zip(feats.names, coefs))
    assert 25 * got["E:juggernaut>ranged"] == pytest.approx(-3, abs=0.8)
    others = [abs(v) for p, v in got.items() if p != "E:juggernaut>ranged"]
    assert 25 * max(others) < 1.2


def test_recovers_a_planted_kit_effect():
    labels = {i: arch(TOP[i % 5]) for i in range(30)}
    kit = {(i, "top"): KitEntry({"auto_denial": 3 if i % 3 == 0 else 0, "auto_reliant": 3 if i % 3 == 1 else 0}, True)
           for i in labels}
    feats = features(labels, kit)
    planted = {"K:auto_denial*auto_reliant": 2 / 25}
    a, b, n, wins = simulate(feats, planted, 400)
    coefs = fit_role(feats, a, b, n, wins, np.zeros(30), np.zeros(len(feats.names)), prior_sd_pp=1.5)
    assert 25 * dict(zip(feats.names, coefs))["K:auto_denial*auto_reliant"] == pytest.approx(2, abs=0.7)


def test_predictions_are_antisymmetric_and_a_mirror_is_zero():
    labels = {0: arch("ranged"), 1: arch("juggernaut", "diver"), 2: arch("ranged")}
    kit = {(i, "top"): KitEntry({"auto_denial": i, "auto_reliant": 3 - i}, True) for i in labels}
    feats = features(labels, kit)
    coefs = np.array([random.Random(i).uniform(-0.1, 0.1) for i in range(len(feats.names))])
    m = feats.matrix(coefs)
    assert m == pytest.approx(-m.T)
    assert m[0, 0] == 0 and m[0, 2] == pytest.approx(
        feats.pair_features(np.array([0]), np.array([2]))[0] @ coefs)  # same archetype: only the kit differs
    assert m[0, 1] == pytest.approx(feats.pair_features(np.array([0]), np.array([1]))[0] @ coefs)


def test_secondary_counts_at_a_third_and_unreviewed_blends_towards_the_role():
    feats = features({0: arch("juggernaut", "diver"), 1: arch("ranged"), 2: arch("ranged", reviewed=False)})
    j, d, r = (feats.words.index(w) for w in ("juggernaut", "diver", "ranged"))
    assert feats.weights[0, j] == 1.0 and feats.weights[0, d] == pytest.approx(0.3)
    avg = (0 + 1 + 1) / 3
    assert feats.weights[2, r] == pytest.approx(0.5 * 1 + 0.5 * avg)
    assert feats.weights[2, j] == pytest.approx(0.5 * 0 + 0.5 * (1 / 3))


def test_with_no_data_the_fit_is_the_prior():
    feats = features({0: arch("ranged"), 1: arch("juggernaut")})
    prior = np.array([0.02 * i for i in range(len(feats.names))])
    empty = np.array([], dtype=int)
    got = fit_role(feats, empty, empty, np.array([]), np.array([]), np.zeros(2), prior, prior_sd_pp=1.5)
    assert got == pytest.approx(prior)


def test_interactions_only_use_archetypes_the_role_has():
    def names(role):
        return param_names(ARCHETYPES[role], interactions_for(ARCHETYPES[role]))

    assert "K:max_hp_damage*arch:tank" in names("top")
    assert not [p for p in names("mid") if "tank" in p]
    assert "K:anti_dive*arch:assassin" in names("mid")
    assert "K:anti_dive*arch:diver" in names("top")


# ---------- test 4: empirical Bayes ----------
def _pairs_with_spread(sd, n=500, pairs=400, seed=3):
    rng = np.random.default_rng(seed)
    p = sigmoid(0.0) + rng.normal(0, sd, pairs)
    return np.full(pairs, float(n)), rng.binomial(n, np.clip(p, 0.01, 0.99)).astype(float), np.full(pairs, 0.5)


def test_empirical_bayes_trusts_champion_data_more_when_it_varies_more():
    low = empirical_k(*_pairs_with_spread(0.012))
    high = empirical_k(*_pairs_with_spread(0.04))
    assert low > high
    assert empirical_k(*_pairs_with_spread(0.0)) == 400  # nothing but noise: the upper clamp
    assert empirical_k(*_pairs_with_spread(0.3)) == 50  # the lower clamp
    assert empirical_k(np.array([]), np.array([]), np.array([])) == 200  # no data: the old k


# ---------- written priors ----------
def test_written_priors_follow_the_guides_as_directions():
    pri = load_archetype_priors()
    game = priors_for(pri, "top", "game")
    assert game[("ranged", "juggernaut")] == pytest.approx(0.5)  # + = the row archetype is favoured
    assert game[("juggernaut", "ranged")] == pytest.approx(-0.5)
    assert priors_for(pri, "top", "lane")[("juggernaut", "tank")] == pytest.approx(0.5)
    assert priors_for(pri, "jungle", "game")[("tempo_ganker", "farmer")] == pytest.approx(0.25)  # half size
    assert priors_for(pri, "adc", "lane")[("poke", "lane_bully")] == pytest.approx(0.25)  # "slightly"
    assert priors_for(pri, "adc", "game")[("scaler", "lane_bully")] == 0.0
    mid = priors_for(pri, "mid", "game")
    assert mid == priors_for(pri, "mid", "lane") and len(mid) == 20  # the cycle: 10 pairs, both directions
    assert priors_for(pri, "support", "game") == {}
    assert all(p["source"] == "video" and p["reviewed"] is False for ps in pri.values() for p in ps)


@pytest.mark.parametrize("line", [
    "{row: wizard, col: tank, dir: '+', target: game}",
    "{row: tank, col: tank, dir: '+', target: game}",
    "{row: ranged, col: tank, dir: 'up', target: game}",
    "{row: ranged, col: tank, dir: '+', target: teamfight}",
])
def test_bad_priors_are_rejected(tmp_path, line):
    path = tmp_path / "bad.yaml"
    path.write_text(f"top:\n  priors:\n    - {line}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_archetype_priors(path)


def test_contradicting_priors_are_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("top:\n  priors:\n    - {row: ranged, col: tank, dir: '+', target: game}\n"
                    "    - {row: tank, col: ranged, dir: '+', target: game}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_archetype_priors(path)


# ---------- the database path ----------
def _store_match(s, match_id, picks_blue, picks_red, blue_win, patch="16.21", bracket="gold"):
    s.add(CrawledMatch(match_id=match_id, patch=patch, bracket=bracket, queue_id=420, game_start_ms=0,
                       game_duration_s=1800))
    roles = ("top", "jungle", "mid", "adc", "support")
    for team, champs, win in ((100, picks_blue, blue_win), (200, picks_red, not blue_win)):
        for champ, role in zip(champs, roles):
            s.add(MatchParticipant(match_id=match_id, champ_id=champ, team_id=team, role=role, win=win))


@pytest.fixture
def crawled(engine):
    """Top is a cycle at 60/40: ranged > juggernaut > tank > ranged. It has to be a cycle: E only sees what strength
    can't explain, and a one-way effect is just "that champion is stronger". Other roles: a fixed filler per team."""
    with Session(engine) as s:
        for c in (1, 2, 3, 4, 5, 6):  # top laners
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1", tags=["Fighter"]))
        for c in range(10, 18):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1", tags=["Mage"]))
        s.commit()
        rng = random.Random(5)
        juggernauts, ranged, tanks = (1, 2), (3, 4), (5, 6)
        for i in range(1, 3001):
            left, right, p_left = rng.choice(((juggernauts, ranged, 0.40), (juggernauts, tanks, 0.60),
                                              (ranged, tanks, 0.40)))
            blue = (rng.choice(left), 10, 11, 12, 13)
            red = (rng.choice(right), 14, 15, 16, 17)
            _store_match(s, f"EUW1_{i}", blue, red, rng.random() < p_left)
        s.commit()
    from draftassist.data.aggregate import aggregate

    tags = {1: "juggernaut", 2: "juggernaut", 3: "ranged", 4: "ranged", 5: "tank", 6: "tank"}
    from draftassist.db.models import ChampionTag

    with Session(engine) as s:
        for c, a in tags.items():
            s.add(ChampionTag(champ_id=c, role="top", archetype=a, reviewed_arch=True))
        s.commit()
    aggregate(engine, "16.21")
    return engine


def _gold(db, model):
    with Session(db) as s:
        return bracket_stats(s, "16.21", "gold", None, model)[0]


def test_aggregation_stores_the_fit_and_it_loads_back(crawled):
    model = load_matchup_model(crawled)
    assert model is not None and model.window == "16.21"
    coef = model.coefs("top")["E:juggernaut>ranged"]
    assert 25 * coef < -1.0  # well past its -0.5pp prior, towards the planted effect
    assert 50 <= model.k("top") <= 400


def test_no_fit_stored_means_no_model(engine):
    assert load_matchup_model(engine) is None


def test_provider_estimates_a_pair_that_never_met_and_reports_confidence(crawled):
    gold = _gold(crawled, load_matchup_model(crawled))
    assert gold.strength_in_matchups
    seen = gold.matchup_wr("top", 3, 1)
    assert seen > 0.5
    assert 0 < gold.matchup_confidence("top", 3, 1) < 1
    assert gold.matchup_wr("top", 1, 3) == pytest.approx(1 - seen)
    # 1 and 2 are on the same team in every game: never met, and still estimated from their strength
    assert gold.matchup_confidence("top", 1, 2) == 0
    assert gold.matchup_wr_or_none("top", 1, 2) is not None


def test_provider_without_a_model_keeps_the_old_formula(crawled):
    gold_old = _gold(crawled, None)
    assert not gold_old.strength_in_matchups
    with Session(crawled) as s:
        m = s.exec(select(Matchup).where(Matchup.role == "top", Matchup.bracket == "gold")).first()
    assert gold_old.matchups[("top", m.champ_a, m.champ_b)] == pytest.approx(shrink(m.wins_a, m.games, 200))


def test_report_prints_e_k_and_k_r(crawled):
    text = format_matchup_report(crawled)
    assert "TOP" in text and "k_r" in text and "juggernaut" in text and "auto_denial" in text


# ---------- no double counting of strength in P(win) ----------
def test_matchup_edge_removes_strength_only_when_the_matchup_includes_it():
    meta = {1: {"top": RoleStat(0.55, 0.1)}, 2: {"top": RoleStat(0.45, 0.1)}}
    wr = sigmoid(logit(0.55) - logit(0.45) + 0.1)
    plain = BracketStats(meta, {("top", 1, 2): wr})
    assert plain.matchup_edge("top", 1, 2) == pytest.approx(logit(wr))  # fixtures: unchanged
    new = BracketStats(meta, {("top", 1, 2): wr}, strength_in_matchups=True)
    assert new.matchup_edge("top", 1, 2) == pytest.approx(0.1)
    assert new.matchup_edge("top", 2, 1) == pytest.approx(-0.1)
    assert new.matchup_edge("top", 1, 3) == 0.0  # no estimate: no edge


def test_matchup_edge_uses_the_strength_the_prior_used():
    meta = {1: {"top": RoleStat(0.60, 0.1)}, 2: {"top": RoleStat(0.40, 0.1)}}
    strength = {(1, "top"): 0.1, (2, "top"): -0.1}  # shrunk harder than the engine's role win rates
    wr = sigmoid(0.2 + 0.05)
    stats = BracketStats(meta, {("top", 1, 2): wr}, strength_in_matchups=True, matchup_strength=strength)
    assert stats.matchup_edge("top", 1, 2) == pytest.approx(0.05)


def test_strength_in_the_prior_comes_from_many_games_not_a_few():
    # A champion on a 20-game hot streak: the engine's role win rate jumps (k = 50); the matchup prior barely moves.
    with_boot = Bootstrap("16.21", {(1, "top"): 0.50, (2, "top"): 0.50}, {(1, "top"): 0.1, (2, "top"): 0.1}, {})
    from draftassist.data.provider import strength_win_rate

    assert strength_win_rate(20, 20, 0.50) == pytest.approx((20 + 500) / 1020)
    assert strength_win_rate(0, 0, 0.53) == pytest.approx(0.53)  # no games: OP.GG's rate
    assert with_boot.win_rate[(1, "top")] == 0.50


def test_lane_term_of_p_win_is_the_edge_not_the_strength_again():
    # Two champions with no interaction: the matchup equals the strength difference, so the lane term must be 0.
    meta = {1: {"top": RoleStat(0.60, 0.5)}, 2: {"top": RoleStat(0.40, 0.5)}}
    wr = sigmoid(logit(0.60) - logit(0.40))
    stats = BracketStats(meta, {("top", 1, 2): wr}, strength_in_matchups=True)
    team = OurTeam.from_tiers({"top": {1: "main"}}, dict.fromkeys(("top", "jungle", "mid", "adc", "support"), "gold"))
    engine = DraftEngine(EngineData({"gold": stats}, {1: "A", 2: "B"}), team)
    terms = engine._terms([1], {1: "top"}, [2], {2: "top"})
    assert terms.lane == pytest.approx(0.0)


# ---------- the crawler looks back only as far as the patch ----------
class _Riot:
    def __init__(self):
        self.start = None

    def match_ids(self, puuid, queue, start_time, count, end_time=None):
        self.start, self.end, self.count = start_time, end_time, count
        return []


def test_crawler_stops_listing_games_older_than_the_patch(engine):
    from draftassist.data.crawler import LOOKBACK_S, PATCH_START_SLACK_S, Crawler
    from draftassist.db.models import SeedPlayer

    now = 1_793_000_000
    with Session(engine) as s:
        s.add(SeedPlayer(puuid="p", bracket="gold", tier="GOLD", division="I"))
        s.commit()
    riot = _Riot()
    Crawler(riot, engine, "16.22", brackets=("gold",), clock=lambda: now).run(max_matches=1)
    assert riot.start == now - LOOKBACK_S  # nothing on this patch yet: the full look-back
    start_ms = (now - 3 * 86_400) * 1000
    with Session(engine) as s:
        s.add(CrawledMatch(match_id="EUW1_1", patch="16.22", bracket="gold", queue_id=420, game_start_ms=start_ms))
        row = s.get(SeedPlayer, "p")
        row.last_crawled_at = None
        s.commit()
    riot = _Riot()
    Crawler(riot, engine, "16.22", brackets=("gold",), clock=lambda: now).run(max_matches=1)
    assert riot.start == start_ms // 1000 - PATCH_START_SLACK_S


def test_crawling_an_older_patch_stops_where_the_next_one_starts(engine):
    from draftassist.data.crawler import Crawler
    from draftassist.db.models import SeedPlayer

    now = 1_793_000_000
    new_start = (now - 86_400) * 1000
    with Session(engine) as s:
        s.add(SeedPlayer(puuid="p", bracket="gold", tier="GOLD", division="I"))
        s.add(CrawledMatch(match_id="EUW1_2", patch="16.22", bracket="gold", queue_id=420, game_start_ms=new_start))
        s.add(CrawledMatch(match_id="EUW1_3", patch="16.22", bracket="gold", queue_id=420, game_start_ms=new_start + 9))
        s.commit()
    riot = _Riot()
    Crawler(riot, engine, "16.21", brackets=("gold",), clock=lambda: now).run(max_matches=1, ids_per_queue=100)
    assert riot.end == new_start // 1000 and riot.count == 100
    riot = _Riot()
    Crawler(riot, engine, "16.22", brackets=("gold",), clock=lambda: now).run(max_matches=1)
    assert riot.end is None  # the newest patch has no end


def test_riot_client_sends_the_end_time(tmp_path):
    from draftassist.data.riot import RiotClient

    sent = {}

    class Client(RiotClient):
        def get(self, host, path, params=None, ttl=None):
            sent.update(params)
            return []

    riot = Client.__new__(Client)
    riot.match_ids("p", 420, 100, 20, end_time=200)
    assert sent["startTime"] == 100 and sent["endTime"] == 200
    sent.clear()
    riot.match_ids("p", 420, 100, 20)
    assert "endTime" not in sent


def test_aggregating_an_old_patch_refreshes_the_window_live_reads(crawled):
    from draftassist.data.aggregate import aggregate
    from draftassist.data.synergy import latest_window
    from draftassist.db.models import ChampRoleStats, MatchupFit, SynergyRoleStats

    with Session(crawled) as s:
        _store_match(s, "EUW1_extra", (1, 10, 11, 12, 13), (3, 14, 15, 16, 17), True)
        s.commit()
    aggregate(crawled, "16.21", window="16.22")
    assert latest_window(crawled) == "16.22"
    with Session(crawled) as s:
        # the slice of the patch that was crawled
        assert s.get(ChampRoleStats, ("16.21", "gold", 1, "top")).games > 0
        assert not s.exec(select(ChampRoleStats).where(ChampRoleStats.patch == "16.22")).all()
        # the pooled tables of the window, not rebuilt for the old patch
        pooled = s.exec(select(SynergyRoleStats).where(SynergyRoleStats.window == "16.22",
                                                       SynergyRoleStats.bracket == "all",
                                                       SynergyRoleStats.champ_id == 1)).one()
        old = s.exec(select(SynergyRoleStats).where(SynergyRoleStats.window == "16.21",
                                                    SynergyRoleStats.bracket == "all",
                                                    SynergyRoleStats.champ_id == 1)).one()
        assert pooled.games == old.games + 1
        assert s.exec(select(MatchupFit).where(MatchupFit.window == "16.22")).first() is not None


# ---------- budgets ----------
def test_building_the_matchups_stays_cheap(crawled):
    t = time.perf_counter()
    load_engine_inputs(crawled)
    model = load_matchup_model(crawled)
    _gold(crawled, model)
    assert time.perf_counter() - t < 2.0


def test_load_engine_inputs_can_turn_the_model_off(crawled):
    assert load_engine_inputs(crawled, use_matchup_model=False) is not None
