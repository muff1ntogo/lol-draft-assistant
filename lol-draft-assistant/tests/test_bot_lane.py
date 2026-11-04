"""Stage 5 of docs/matchup-archetypes.md: bot lane is 2v2."""
from __future__ import annotations

import numpy as np
import pytest
from sqlmodel import Session

from draftassist.data.aggregate import aggregate
from draftassist.data.archetypes import ARCHETYPES
from draftassist.data.matchup_model import load_matchup_model
from draftassist.db.models import Champion, ChampionTag, CrawledMatch, MatchParticipant
from draftassist.engine.bot_lane import BotLane, BotLaneData, bot_features, fit_bot_game, fit_bot_gold
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.lane_ban import MappingLaneData
from draftassist.engine.model import ROLES, BracketStats, EngineConfig, EngineData, OurTeam, RoleStat
from draftassist.engine.state import DraftState

ADC, SUP = ARCHETYPES["adc"], ARCHETYPES["support"]
A, S = len(ADC), len(SUP)


def one_hot(index, size):
    out = np.zeros(size)
    out[index] = 1.0
    return out


def tables(cross=None, pair=None, kind="game", **kw):
    """A BotLane with four champions: ADC 1 scaler, 2 lane bully; support 3 engage, 4 enchanter."""
    zero = np.zeros((A, S))
    c, p = np.array(cross if cross is not None else zero), np.array(pair if pair is not None else zero)
    return BotLane({1: one_hot(ADC.index("scaler"), A), 2: one_hot(ADC.index("lane_bully"), A)},
                   {3: one_hot(SUP.index("engage"), S), 4: one_hot(SUP.index("enchanter"), S)},
                   c if kind == "game" else zero, p if kind == "game" else zero,
                   c if kind == "gold" else zero, p if kind == "gold" else zero,
                   {(2, "adc"): "lane_bully", (1, "adc"): "scaler"}, **kw)


def cell(a, s, value):
    t = np.zeros((A, S))
    t[ADC.index(a), SUP.index(s)] = value
    return t


# ---------- test 1: a planted cross effect is recovered ----------
def _matches(n, seed=1):
    rng = np.random.default_rng(seed)
    pick = lambda size: np.eye(size)[rng.integers(0, size, n)]
    return rng, pick(A), pick(S), pick(A), pick(S)


def test_recovers_a_planted_cross_effect_in_gold():
    # An engage support against a scaler ADC: 200 gold to the support's side (the doc's +2pp lane)
    rng, ab, sb, ar, sr = _matches(40_000)
    x = bot_features(ab, sb, ar, sr)
    planted = np.zeros(x.shape[1])
    planted[ADC.index("scaler") * S + SUP.index("engage")] = -200.0  # the scaler's side loses it
    gold = x @ planted + rng.normal(0, 1000, 40_000)
    got = fit_bot_gold(x, np.zeros(40_000), gold, 1000.0 ** 2)
    assert got[ADC.index("scaler") * S + SUP.index("engage")] == pytest.approx(-200, abs=60)
    assert np.abs(np.delete(got, ADC.index("scaler") * S + SUP.index("engage"))).max() < 90


def test_recovers_a_planted_pair_effect_in_wins_after_strength():
    # At 20k matches the prior leaves about half of a real effect: it takes this many to read it closely
    n = 200_000
    rng, ab, sb, ar, sr = _matches(n, seed=2)
    x = bot_features(ab, sb, ar, sr)
    at = A * S + ADC.index("scaler") * S + SUP.index("enchanter")
    planted = np.zeros(x.shape[1])
    planted[at] = 0.16
    # Enchanters win less whoever they're with: a main effect, in the offset as a champion's own strength would be
    main = -0.1 * (sb[:, SUP.index("enchanter")] - sr[:, SUP.index("enchanter")])
    won = rng.random(n) < 1 / (1 + np.exp(-(x @ planted + main)))
    got = fit_bot_game(x, main, won)
    assert got[at] == pytest.approx(0.13, abs=0.05)
    other_enchanter = A * S + ADC.index("poke") * S + SUP.index("enchanter")
    assert abs(got[other_enchanter]) < 0.05  # the main effect doesn't leak into the pairing


def test_no_data_fits_nothing():
    assert (fit_bot_game(np.zeros((0, 50)), np.zeros(0), np.zeros(0, dtype=bool)) == 0).all()
    assert (fit_bot_gold(np.zeros((0, 50)), np.zeros(0), np.zeros(0), 1e6) == 0).all()


def test_features_are_antisymmetric():
    _, ab, sb, ar, sr = _matches(10)
    assert bot_features(ab, sb, ar, sr) == pytest.approx(-bot_features(ar, sr, ab, sb))


# ---------- reading it ----------
def test_cross_terms_and_unknowns():
    bot = tables(cross=cell("scaler", "engage", -0.2))
    assert bot.cross(1, 3) == pytest.approx(-0.2)
    assert bot.game_cross(1, 4, 2, 3) == pytest.approx(-0.2)  # our scaler v their engage
    assert bot.game_cross(2, 3, 1, 4) == pytest.approx(0.2)  # the mirror: our engage v their scaler
    assert bot.game_cross(1, None, None, None) == 0.0
    assert bot.cross(99, 3) == 0.0  # a champion with no labels


def _engine(bot, role_weights=None):
    meta = {c: {r: RoleStat(0.5, 0.1)} for c, r in ((1, "adc"), (2, "adc"), (3, "support"), (4, "support"))}
    stats = BracketStats(meta, {("adc", 1, 2): 0.5, ("support", 3, 4): 0.5})
    team = OurTeam.from_tiers({"adc": {1: "main", 2: "main"}, "support": {3: "main", 4: "main"}},
                              dict.fromkeys(ROLES, "gold"))
    names = {1: "Scaler", 2: "Bully", 3: "Engager", 4: "Enchanter"}
    return DraftEngine(EngineData({"gold": stats}, names, bot=bot), team, EngineConfig(role_weights=role_weights))


# ---------- test 2: changing our support changes the ADC's bot term only ----------
def test_changing_our_support_changes_only_the_bot_term():
    bot = tables(cross=cell("lane_bully", "enchanter", 0.3))
    eng = _engine(bot)
    # our ADC 1 (scaler) against their 2 (bully): their bully likes facing our enchanter
    with_engage = eng._terms([1, 3], {1: "adc", 3: "support"}, [2], {2: "adc"})
    with_enchanter = eng._terms([1, 4], {1: "adc", 4: "support"}, [2], {2: "adc"})
    assert with_engage.lane == pytest.approx(0.0)
    assert with_enchanter.lane == pytest.approx(-0.3)
    assert with_engage.meta_us == pytest.approx(with_enchanter.meta_us)


def test_pair_terms_stay_out_of_p_win():
    eng = _engine(tables(pair=cell("scaler", "engage", 0.5)))
    assert eng._terms([1, 3], {1: "adc", 3: "support"}, [], {}).lane == 0.0


def test_the_bot_term_scales_by_the_two_roles_mean_weight():
    eng = _engine(tables(cross=cell("scaler", "enchanter", 0.2)), {"adc": 0.5, "support": 1.5})
    assert eng._terms([1], {1: "adc"}, [4], {4: "support"}).lane == pytest.approx(0.2)


# ---------- test 3: with cross and pair zero, it's two independent 1v1s ----------
def test_with_zero_tables_results_are_two_independent_1v1s():
    base = MappingLaneData({"adc": {2: 0.5}, "support": {4: 0.5}}, matchups={("adc", 1, 2): 0.6, ("support", 3, 4): 0.45})
    data = BotLaneData(base, tables(), {"adc": [(1, 1.0)], "support": [(3, 1.0)]}, {"adc": 2, "support": 4}, 0.5)
    assert data.matchup_wr("adc", 1, 2) == pytest.approx(0.6)
    assert data.matchup_wr("support", 3, 4) == pytest.approx(0.45)
    eng_plain, eng_zero = _engine(None), _engine(tables())
    state = DraftState(TOURNAMENT, "blue")
    assert [c.name for c in eng_plain.suggest_bans(state)] == [c.name for c in eng_zero.suggest_bans(state)]


def test_resp_uses_the_partner_and_the_enemy_support():
    base = MappingLaneData({"adc": {2: 0.5}}, matchups={("adc", 1, 2): 0.5})
    partner_good = tables(pair=cell("scaler", "engage", 0.4))
    with_engage = BotLaneData(base, partner_good, {"support": [(3, 1.0)]}, {}, 0.0)
    with_enchanter = BotLaneData(base, partner_good, {"support": [(4, 1.0)]}, {}, 0.0)
    assert with_engage.matchup_wr("adc", 1, 2) > 0.5
    assert with_enchanter.matchup_wr("adc", 1, 2) == pytest.approx(0.5)
    expected = BotLaneData(base, partner_good, {"support": [(3, 0.5), (4, 0.5)]}, {}, 0.0)
    assert 0.5 < expected.matchup_wr("adc", 1, 2) < with_engage.matchup_wr("adc", 1, 2)
    other_roles = BotLaneData(MappingLaneData({"top": {2: 0.5}}, matchups={("top", 1, 2): 0.6}), partner_good, {}, {}, 0.0)
    assert other_roles.matchup_wr("top", 1, 2) == pytest.approx(0.6)


def test_resp_weighs_gold_by_the_lane_weight():
    base = MappingLaneData({"adc": {2: 0.5}}, matchups={("adc", 1, 2): 0.5})
    gold = tables(pair=cell("scaler", "engage", 500.0), kind="gold", slope=0.4)
    assert BotLaneData(base, gold, {"support": [(3, 1.0)]}, {}, 0.0).matchup_wr("adc", 1, 2) == pytest.approx(0.5)
    at_one = BotLaneData(base, gold, {"support": [(3, 1.0)]}, {}, 1.0).matchup_wr("adc", 1, 2)
    assert at_one == pytest.approx(1 / (1 + np.exp(-0.2)))


def test_engine_ban_tables_score_the_adc_with_the_partner():
    bot = tables(pair=cell("scaler", "engage", 1.0))
    eng = _engine(bot)
    state = DraftState(TOURNAMENT, "blue")
    plain = {r.opp: r for r in _engine(None).lane_ban_tables(state)["adc"].rows}
    table = {r.opp: r for r in eng.lane_ban_tables(state)["adc"].rows}
    assert table[2].resp != plain[2].resp  # the expected partner, from the support pool


def test_reason_names_the_partner_when_the_pairing_matters():
    eng = _engine(tables(pair=cell("lane_bully", "engage", 0.1)))
    text = eng._pick_reason(2, "adc", {2: "adc", 3: "support"}, {}, [], 0.0, 0.0, None)
    assert text == "ADC main, Your Engager: lane bully partner +2.5pp"


def test_mage_bot_uses_its_bot_lane_archetype(engine):
    with Session(engine) as s:
        s.add(Champion(champ_id=30, key="Karthus", name="Karthus", patch="16.21.1", tags=["Mage"]))
        s.add(ChampionTag(champ_id=30, role="adc", archetype="mage_bot", reviewed_arch=True))
        s.add(ChampionTag(champ_id=30, role="jungle", archetype="farmer", reviewed_arch=True))
        s.add(CrawledMatch(match_id="EUW1_1", patch="16.21", bracket="gold", queue_id=420, game_start_ms=0,
                           game_duration_s=1800))
        s.commit()
        fillers = list(range(40, 49))
        for c in fillers:
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1", tags=["Fighter"]))
        s.commit()
        for i, (champ, role, team) in enumerate(((30, "adc", 100), (40, "top", 100), (41, "jungle", 100), (42, "mid", 100),
                                                 (43, "support", 100), (44, "top", 200), (45, "jungle", 200),
                                                 (46, "mid", 200), (47, "adc", 200), (48, "support", 200))):
            s.add(MatchParticipant(match_id="EUW1_1", champ_id=champ, team_id=team, role=role, win=team == 100))
        s.commit()
    aggregate(engine, "16.21")
    bot = load_matchup_model(engine).bot()
    assert bot.adc[30][ADC.index("mage_bot")] == pytest.approx(1.0)
    assert bot.pair_label(30, "adc") == "mage_bot"


# ---------- recap ----------
def test_recap_combines_the_two_lanes():
    from draftassist.api.recap import bot_lane_summary

    adc = {"delta_pp": 2.0, "lane_gold": 300, "games": 10}
    sup = {"delta_pp": -0.5, "lane_gold": -100, "games": 20}
    got = bot_lane_summary(adc, sup, cross_pp=0.25)
    assert got["delta_pp"] == 1.8 and got["lane_gold"] == 200 and got["band"] == "ours" and got["games"] == 10
    assert bot_lane_summary({"delta_pp": None}, sup, 0.0) is None
    assert bot_lane_summary(adc, {"delta_pp": 1.0, "lane_gold": None, "games": 5}, 0.0)["lane_gold"] is None


def test_health_and_the_tree_stay_json_with_bot_tables():
    # Real data has bot tables and fixtures didn't: a numpy value here was a 500 on Pools and the Matchup tree
    import json

    from draftassist.engine.blind import pool_health
    from draftassist.engine.matchup_tree import matchup_tree

    eng = _engine(tables(pair=cell("scaler", "engage", 0.4), cross=cell("lane_bully", "engage", 0.1)))
    for role in ("adc", "support"):
        json.dumps(pool_health(eng, role))
        json.dumps(matchup_tree(eng, role, ARCHETYPES[role]))
    wr = BotLaneData(MappingLaneData({"adc": {2: 0.5}}, matchups={("adc", 1, 2): 0.5}), tables(pair=cell("scaler", "engage", 0.4)),
                     {"support": [(3, 1.0)]}, {}, 0.0).matchup_wr("adc", 1, 2)
    assert type(wr) is float

