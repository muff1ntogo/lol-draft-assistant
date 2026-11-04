"""Stage 6 of docs/matchup-archetypes.md: blind picks, counter picks, who starts the fight, pool health."""
from __future__ import annotations

import pytest

from draftassist.data.rules import TEAM_RULES_FILE, load_team_rules
from draftassist.data.tags import TAGS_FILE, load_tags_yaml
from draftassist.engine.blind import blind_safety, blind_table, pool_health
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.mathx import sigmoid
from draftassist.engine.model import ROLES, BracketStats, EngineConfig, EngineData, OurTeam, RoleStat
from draftassist.engine.state import SKIPPED, DraftState
from draftassist.engine.team import TeamModel, parse_team_rules
from draftassist.engine.traits import IDENTITY_FREE_TAGS, TAGS, ChampionTraits, KitEntry, KitTraits, TagEntry

BRACKETS = dict.fromkeys(ROLES, "gold")


def tag(**v):
    return TagEntry({**dict.fromkeys(TAGS, 0), **v}, reviewed=True)


def _engine(meta_champs, matchups, pool, measured=None, kit=None, cfg=None, role="mid"):
    meta = {c: {role: RoleStat(0.5, rate)} for c, rate in meta_champs.items()}
    traits = ChampionTraits(measured or {}, {}, {}, KitTraits(kit or {}))
    stats = BracketStats(meta, matchups)
    names = {c: f"C{c}" for c in meta_champs}
    return DraftEngine(EngineData({"gold": stats}, names, traits=traits),
                       OurTeam.from_tiers({role: pool}, BRACKETS), cfg or EngineConfig(beta=0.0))


# ---------- test 1: blind safety ----------
def test_blind_safety_prefers_a_comfortable_safe_pick_over_a_meta_one_being_learned():
    opponents = {c: 0.1 for c in range(10, 16)}
    matchups = {("mid", 1, o): 0.51 for o in opponents} | {("mid", 2, o): 0.53 for o in opponents}
    matchups |= {("mid", 2, 10): 0.40, ("mid", 2, 11): 0.41}  # the meta pick has two hard counters
    measured = {(1, "mid"): {"behind_value": 0.47}, (2, "mid"): {"behind_value": 0.38}}
    engine = _engine({1: 0.05, 2: 0.2, **opponents}, matchups, {1: "main", 2: "learning"}, measured)
    table = blind_table(engine, "mid")
    assert blind_safety(engine, 1, "mid") > blind_safety(engine, 2, "mid")
    assert table[1].parts["worst"] > table[2].parts["worst"]
    assert table[1].parts["behind"] > table[2].parts["behind"]
    assert table[1].comfort > table[2].comfort


def test_a_part_with_no_data_counts_as_average():
    engine = _engine({1: 0.1, 2: 0.1, 10: 0.1}, {("mid", 1, 10): 0.55, ("mid", 2, 10): 0.45}, {1: "main"})
    assert blind_table(engine, "mid")[1].parts["behind"] == 0.0


def test_opponents_under_the_pick_rate_floor_do_not_count():
    engine = _engine({1: 0.1, 2: 0.1, 10: 0.1, 99: 0.001},
                     {("mid", 1, 10): 0.5, ("mid", 1, 99): 0.1, ("mid", 2, 10): 0.5, ("mid", 2, 99): 0.9}, {1: "main"})
    table = blind_table(engine, "mid")
    assert table[1].raw["matchup"] == pytest.approx(table[2].raw["matchup"])


def test_kit_reads_range_and_escape():
    kit = {(1, "mid"): KitEntry({"ability_range": 3, "escape": 3}, True), (2, "mid"): KitEntry({}, True)}
    engine = _engine({1: 0.1, 2: 0.1}, {}, {1: "main"}, kit=kit)
    assert blind_table(engine, "mid")[1].raw["kit"] == pytest.approx(1.0)
    assert blind_table(engine, "mid")[1].parts["kit"] > 0 > blind_table(engine, "mid")[2].parts["kit"]


def test_weights_come_from_the_config_and_a_zero_weight_drops_a_part():
    kit = {(1, "mid"): KitEntry({"ability_range": 3}, True), (2, "mid"): KitEntry({}, True)}
    cfg = EngineConfig(beta=0.0, blind_weights={"kit": 0.0, "comfort": 0.0})
    engine = _engine({1: 0.1, 2: 0.1}, {}, {1: "main"}, kit=kit, cfg=cfg)
    assert blind_table(engine, "mid")[1].total == pytest.approx(0.0)


def test_synergy_partners_are_the_current_pools_weighted_by_tier():
    from draftassist.engine.synergy import PairScore, SynergyTable

    def synergy(top_with_a, top_with_b):
        pair = lambda s: PairScore(s, s, 100, False)
        return SynergyTable("16.21", "all", {(("mid", 1), ("top", 5)): pair(top_with_a), (("mid", 1), ("top", 6)): pair(top_with_b)}, {})

    meta = {1: {"mid": RoleStat(0.5, 0.1)}, 2: {"mid": RoleStat(0.5, 0.1)}, 5: {"top": RoleStat(0.5, 0.1)}, 6: {"top": RoleStat(0.5, 0.1)}}
    def build(pool):
        return DraftEngine(EngineData({"gold": BracketStats(meta, {})}, {}, synergy=synergy(0.3, 0.0)),
                           OurTeam.from_tiers({"mid": {1: "main"}, "top": pool}, BRACKETS), EngineConfig(beta=0.0))
    with_main = blind_table(build({5: "main", 6: "learning"}), "mid")[1].raw["synergy"]
    with_learning = blind_table(build({5: "learning", 6: "main"}), "mid")[1].raw["synergy"]
    assert with_main > with_learning > 0  # a pool change moves it, with no refit


# ---------- test 2: the comfort gate ----------
def _gate_engine(learning_edge_pp, main_edge_pp=-0.5):
    """Mid: our main 1 and learning 2 against their locked 3."""
    meta = {c: {"mid": RoleStat(0.5, 0.1)} for c in (1, 2, 3)}
    wr = lambda pp: sigmoid(pp / 25)
    stats = BracketStats(meta, {("mid", 1, 3): wr(main_edge_pp), ("mid", 2, 3): wr(learning_edge_pp)})
    team = OurTeam.from_tiers({"mid": {1: "main", 2: "learning"}}, BRACKETS)
    return DraftEngine(EngineData({"gold": stats}, {1: "Ahri", 2: "Syndra", 3: "Zed"}), team, EngineConfig(beta=0.0))


def _state_they_locked_mid(engine):
    # we're red; blue's first pick is 3, which only has stats in mid
    return DraftState(TOURNAMENT, "red", (SKIPPED,) * 6 + (3,))


def test_the_comfort_gate_keeps_a_main_above_a_small_learning_edge():
    engine = _gate_engine(2.0)
    picks = engine.suggest_picks(_state_they_locked_mid(engine), k=2, role="mid")
    assert [p.champ_id for p in picks] == [1, 2]
    assert "Your main Ahri" in picks[0].reason and "Syndra (learning, +2.0pp)" in picks[0].reason


def test_a_large_learning_edge_still_wins():
    engine = _gate_engine(3.5)
    picks = engine.suggest_picks(_state_they_locked_mid(engine), k=2, role="mid")
    assert [p.champ_id for p in picks] == [2, 1]
    assert "Your main" not in picks[1].reason


def test_a_main_losing_badly_does_not_hold_the_learning_champion_back():
    engine = _gate_engine(2.0, main_edge_pp=-3.0)
    picks = engine.suggest_picks(_state_they_locked_mid(engine), k=2, role="mid")
    assert picks[0].champ_id == 2


def test_the_gate_changes_pick_value_not_the_win_chance():
    engine = _gate_engine(2.0)
    state = _state_they_locked_mid(engine)
    picks = {p.champ_id: p for p in engine.suggest_picks(state, k=2, role="mid")}
    assert picks[2].win_prob > picks[1].win_prob  # P(win) itself isn't gated


def test_the_gate_shrinks_a_positive_matchup_by_the_tier_factor():
    wide_open = {"main": 1.0, "comfortable": 1.0, "learning": 1.0}
    state = _state_they_locked_mid(_gate_engine(5.0))
    def value(gate):
        e = _gate_engine(5.0)
        e = DraftEngine(e.data, e.team, EngineConfig(beta=0.0, comfort_gate=gate))
        return next(p for p in e.suggest_picks(state, k=2, role="mid") if p.champ_id == 2).value
    assert value(wide_open) - value({"main": 1.0, "comfortable": 0.7, "learning": 0.2}) == pytest.approx(0.25 * 0.8 * 0.2)


# ---------- pick order ----------
def test_early_slots_favour_blind_safety():
    from draftassist.engine.plan import _PickCosts, our_slots

    slots = our_slots(TOURNAMENT, "blue")
    base = dict(role="mid", loss=0.0, counter=None, edge=0.0, roles=1, ban_q=0.0, presence=0.0, risk_weight=0.1,
                risk_horizon=3, flex_bonus=0.02)
    safe, risky = _PickCosts(1, blind=3.0, **base), _PickCosts(2, blind=-3.0, **base)
    assert safe.score(slots[0]) > safe.score(slots[-1])
    assert risky.score(slots[0]) < risky.score(slots[-1])


# ---------- test 3: who starts the fight ----------
def _team_model(tags):
    traits = ChampionTraits({}, tags, {})
    rules = load_team_rules(TEAM_RULES_FILE)
    return TeamModel(rules, traits, {r.id: r.prior_pp / 25 for r in rules})


COMP_FOLLOW = {1: "top", 2: "jungle", 3: "mid", 4: "adc", 5: "support"}


def _comp(role_champ):
    return [(c, r) for c, r in role_champ.items()]


def test_followup_without_setup_lowers_team_and_a_setup_champion_raises_it():
    only = {(c, r): tag(followup=3) for c, r in COMP_FOLLOW.items()}
    with_setup = {**only, (2, "jungle"): tag(setup=3)}
    plain = {(c, r): tag() for c, r in COMP_FOLLOW.items()}
    assert _team_model(only).team(_comp(COMP_FOLLOW)) < _team_model(plain).team(_comp(COMP_FOLLOW))
    assert _team_model(with_setup).team(_comp(COMP_FOLLOW)) > _team_model(only).team(_comp(COMP_FOLLOW))


def test_has_fight_starter_replaces_has_engage_and_keeps_its_prior():
    rules = {r.id: r for r in load_team_rules(TEAM_RULES_FILE)}
    assert "has_engage" not in rules
    assert rules["has_fight_starter"].prior_pp == 1.5
    assert "layered_engage" in rules
    engager = {(c, r): tag() for c, r in COMP_FOLLOW.items()} | {(2, "jungle"): tag(engage=3)}
    setter = {(c, r): tag() for c, r in COMP_FOLLOW.items()} | {(2, "jungle"): tag(setup=3)}
    rule = parse_team_rules([{"id": "x", "feature": {"max_any": ["engage", "setup"]}, "prior_pp": 25.0}])
    one = TeamModel(rule, ChampionTraits({}, engager, {}), {"x": 1.0})
    two = TeamModel(rule, ChampionTraits({}, setter, {}), {"x": 1.0})
    assert one.features([_comp(COMP_FOLLOW)])[0, 0] == pytest.approx(1.0)
    assert two.features([_comp(COMP_FOLLOW)])[0, 0] == pytest.approx(1.0)


def test_needs_team_is_rewarded_with_help_and_penalised_without():
    rules = {r.id: r for r in load_team_rules(TEAM_RULES_FILE)}
    assert rules["needs_followup"].prior_pp > 0 and rules["needs_team_alone"].prior_pp < 0
    plain = {(c, r): tag() for c, r in COMP_FOLLOW.items()}
    alone = {**plain, (3, "mid"): tag(needs_team=3)}
    helped = {**alone, (2, "jungle"): tag(dive=3)}
    assert _team_model(alone).team(_comp(COMP_FOLLOW)) < _team_model(plain).team(_comp(COMP_FOLLOW))
    assert _team_model(helped).team(_comp(COMP_FOLLOW)) > _team_model(alone).team(_comp(COMP_FOLLOW))


def test_the_new_tags_stay_out_of_identities():
    from draftassist.engine.comp import IDENTITY_TERMS

    used = {t for terms in IDENTITY_TERMS.values() for t, _ in terms}
    assert set(IDENTITY_FREE_TAGS) == {"setup", "followup", "needs_team"}
    assert not used & set(IDENTITY_FREE_TAGS)


def test_shipped_draft_has_the_docs_examples_and_keeps_the_ratings():
    raw = load_tags_yaml(TAGS_FILE)
    for key, role in (("Vi", "jungle"), ("Alistar", "support"), ("Leona", "support"), ("Syndra", "mid"), ("Ashe", "adc")):
        assert raw[key][role].values["setup"] >= 2, key
    for key, role in (("Orianna", "mid"), ("Yasuo", "mid"), ("MissFortune", "adc"), ("Katarina", "mid"), ("Viktor", "mid")):
        assert raw[key][role].values["followup"] >= 2, key
    for key, role in (("Ryze", "mid"), ("Orianna", "mid"), ("Azir", "mid")):
        assert raw[key][role].values["needs_team"] >= 2, key
    assert sum(e.rated for by_role in raw.values() for e in by_role.values()) == 247


# ---------- test 4: the pool health card ----------
def test_health_flags_a_pool_with_no_blind_safe_champion():
    opponents = {c: 0.1 for c in range(10, 14)}
    matchups = {("mid", 1, o): 0.40 for o in opponents} | {("mid", 2, o): 0.60 for o in opponents}
    engine = _engine({1: 0.1, 2: 0.1, **opponents}, matchups, {1: "learning"})
    health = pool_health(engine, "mid")
    assert health["blind"]["name"] == "C1" and health["blind"]["ok"] is False
    good = _engine({1: 0.1, 2: 0.1, **opponents}, matchups, {2: "main"})
    assert pool_health(good, "mid")["blind"]["ok"] is True


def test_health_reports_setup_and_counters():
    meta = {c: {"mid": RoleStat(0.5, 0.1)} for c in (1, 2, 10, 11, 12)}
    wr = {("mid", 1, 10): 0.40, ("mid", 1, 11): 0.48, ("mid", 1, 12): 0.5, ("mid", 2, 10): 0.55, ("mid", 2, 11): 0.5, ("mid", 2, 12): 0.5}
    traits = ChampionTraits({}, {(2, "mid"): tag(setup=3)}, {})
    engine = DraftEngine(EngineData({"gold": BracketStats(meta, wr)}, {c: f"C{c}" for c in meta}, traits=traits),
                         OurTeam.from_tiers({"mid": {1: "main", 2: "learning"}, "adc": {1: "main"}}, BRACKETS),
                         EngineConfig(beta=0.0))
    h = pool_health(engine, "mid")
    assert h["setup"]["name"] == "C2" and h["setup"]["ok"]
    assert h["counter"]["main"] == "C1" and h["counter"]["ok"]
    assert h["counter"]["worst"][0] == {"name": "C10", "main_pp": -10.0, "answered": True, "answer": "C2", "answer_pp": 5.0}
    assert pool_health(engine, "adc")["setup"]["applies"] is False  # "where the role allows"


def test_an_empty_pool_is_handled():
    engine = _engine({1: 0.1, 2: 0.1}, {}, {})
    h = pool_health(engine, "mid")
    assert h["blind"]["ok"] is False and h["setup"]["ok"] is False and h["counter"]["ok"] is True


# ---------- behind value ----------
def test_behind_value_is_the_win_rate_when_300_gold_behind(engine):
    from sqlmodel import Session

    from test_lane_matchup import FILLER_BLUE, FILLER_RED, _store

    from draftassist.data.aggregate import aggregate
    from draftassist.data.traits import load_traits
    from draftassist.db.models import Champion

    with Session(engine) as s:
        for c in (1, 3, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1", tags=["Fighter"]))
        s.commit()
        for i in range(200):
            behind = i % 2 == 0
            win = i % 8 != 0 if behind else True  # 3 in 4 when 1 is behind; 3 never wins from behind
            _store(s, f"EUW1_{i}", (1, *FILLER_BLUE), (3, *FILLER_RED), win, {"top": -400 if behind else 400})
        s.commit()
    aggregate(engine, "16.21")
    measured = load_traits(engine).measured
    assert measured[(1, "top")]["behind_value"] > 0.5 > measured[(3, "top")]["behind_value"]
    assert (1, "top") in measured


# ---------- the Live check strip ----------
def _live(v_engine, st):
    from draftassist.api.live import _check_strip

    return _check_strip(v_engine, st)


def test_check_strip_reads_the_tags_and_flags_what_is_missing():
    meta = {c: {r: RoleStat(0.5, 0.1)} for c, r in ((1, "top"), (2, "jungle"), (3, "mid"))}
    traits = ChampionTraits({}, {(2, "jungle"): tag(setup=3), (1, "top"): tag()}, {})
    engine = DraftEngine(EngineData({"gold": BracketStats(meta, {})}, {1: "A", 2: "B", 3: "C"}, traits=traits),
                         OurTeam.from_tiers({"top": {1: "main"}, "jungle": {2: "main"}, "mid": {3: "main"}}, BRACKETS),
                         EngineConfig())
    empty = _live(engine, DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6))
    assert empty == {"picking": "early", "fight_starter": False, "frontline": False}
    assert engine.provides_fight_starter(2, "jungle") and not engine.provides_fight_starter(1, "top")


def test_check_strip_says_late_with_few_enemy_picks_left():
    engine = DraftEngine(EngineData({"gold": BracketStats({1: {"top": RoleStat(0.5, 0.1)}}, {})}, {1: "A"},
                                    traits=ChampionTraits({}, {}, {})),
                         OurTeam.from_tiers({"top": {1: "main"}}, BRACKETS), EngineConfig())
    state = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6 + (70, 71, 72, 73, 74, 75) + (SKIPPED,) * 4 + (76, 77, 78))
    assert _live(engine, state)["picking"] == "late"


def test_no_traits_no_strip():
    engine = DraftEngine(EngineData({"gold": BracketStats({1: {"top": RoleStat(0.5, 0.1)}}, {})}, {1: "A"}),
                         OurTeam.from_tiers({"top": {1: "main"}}, BRACKETS), EngineConfig())
    assert _live(engine, DraftState(TOURNAMENT, "blue")) is None


