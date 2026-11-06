"""Stage 4 of docs/matchup-archetypes.md: lane matchup vs game matchup, and how much each role's matchup matters.

The lane matchup is the mean gold lead at 15 minutes, from match-v5 timelines.
"""
from __future__ import annotations

import json
import random
from dataclasses import replace

import numpy as np
import pytest
from sqlmodel import Session, select
from test_matchup_model import arch, features

from draftassist.data.aggregate import aggregate
from draftassist.data.matchup_model import (
    format_matchup_report,
    load_matchup_model,
    load_role_weights,
)
from draftassist.data.settings_store import (
    engine_config,
    get_engine_settings,
    save_engine_settings,
    settings_payload,
)
from draftassist.data.timeline_map import parse_timeline
from draftassist.data.timelines import backfill_timelines, store_timeline
from draftassist.db.models import (
    Champion,
    ChampionTag,
    CrawledMatch,
    MatchParticipant,
    Matchup,
)
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.lane_ban import BracketLaneData, RoleBanTable, team_lane_ban
from draftassist.engine.matchup_model import (
    empirical_k_gold,
    fit_lane_gold,
    fit_lane_slopes,
    fit_role_weights,
)
from draftassist.engine.mathx import sigmoid
from draftassist.engine.model import (
    ROLES,
    BracketStats,
    EngineConfig,
    EngineData,
    OurTeam,
    RoleStat,
)
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import DraftState


def _store(s, match_id, blue, red, blue_win, gold=None, patch="16.21", bracket="gold"):
    """`gold`: role -> blue's gold lead at 15 minutes (red's is the opposite). Roles left out: no lead recorded."""
    gold = gold or {}
    s.add(CrawledMatch(match_id=match_id, patch=patch, bracket=bracket, queue_id=420, game_start_ms=0,
                       game_duration_s=1800, timeline_done=True))
    for team, sign, champs, win in ((100, 1, blue, blue_win), (200, -1, red, not blue_win)):
        for champ, role in zip(champs, ROLES):
            lead = gold.get(role)
            s.add(MatchParticipant(match_id=match_id, champ_id=champ, team_id=team, role=role, win=win,
                                   lane_gold_15=None if lead is None else sign * lead,
                                   lane_xp_15=None if lead is None else sign * lead / 2))


FILLER_BLUE, FILLER_RED = (10, 11, 12, 13), (14, 15, 16, 17)


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        for c in (1, 2, 3, 4, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1", tags=["Fighter"]))
        for c, a in ((1, "tank"), (2, "tank"), (3, "ranged"), (4, "ranged")):
            s.add(ChampionTag(champ_id=c, role="top", archetype=a, reviewed_arch=True))
        s.commit()
        rng = random.Random(11)
        for i in range(1500):
            ours, theirs = rng.choice((1, 2)), rng.choice((3, 4))
            lead = rng.gauss(-400, 800)  # ranged wins lane...
            win_chance = sigmoid(0.2 + 0.8 * lead / 1000 + 0.3)  # ...tanks win the game more than the lane says
            _store(s, f"EUW1_{i}", (ours, *FILLER_BLUE), (theirs, *FILLER_RED), rng.random() < win_chance, {"top": lead})
        s.commit()
    aggregate(engine, "16.21")
    return engine


# ---------- reading a timeline ----------
def _match():
    positions = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
    parts = []
    for i in range(10):
        parts.append({"participantId": i + 1, "championId": 100 + i, "teamId": 100 if i < 5 else 200,
                      "teamPosition": positions[i % 5]})
    return {"info": {"participants": parts}}


def _timeline(minute15=True):
    frames = [{"timestamp": 60_000 * m, "participantFrames": {}} for m in range(15)]
    if minute15:
        frames.append({"timestamp": 900_261, "participantFrames": {
            str(i + 1): {"totalGold": 5000 + 100 * i, "xp": 7000 + 10 * i} for i in range(10)}})
    return {"info": {"frames": frames}}


def _leads(timeline, match):
    return {c: (p["lane_gold_15"], p["lane_xp_15"]) for c, p in parse_timeline(timeline, match).participants.items()}


def test_lead_is_against_the_enemy_in_the_same_position():
    diffs = _leads(_timeline(), _match())
    assert diffs[100] == (5000 - 5500, 7000 - 7050)  # blue top against red top
    assert diffs[105] == (500, 50)
    assert diffs[104] == (5400 - 5900, 7040 - 7090)
    assert len(diffs) == 10


def test_a_game_that_ended_before_15_minutes_has_no_lead():
    assert parse_timeline(_timeline(minute15=False), _match()) is None
    assert parse_timeline(None, _match()) is None


def test_a_lane_without_both_positions_has_no_lead():
    match = _match()
    match["info"]["participants"][0]["teamPosition"] = ""
    diffs = _leads(_timeline(), match)
    assert diffs[100] == (None, None) and diffs[105] == (None, None) and diffs[101][0] is not None


class _Riot:
    def __init__(self, missing=()):
        self.calls, self.missing = [], missing

    def timeline(self, match_id):
        self.calls.append(match_id)
        return None if match_id in self.missing else _timeline()

    def match(self, match_id):
        return _match()


def _unfetched(engine, ids):
    with Session(engine) as s:
        for c in range(100, 110):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1"))
        for start, match_id in enumerate(ids):
            s.add(CrawledMatch(match_id=match_id, patch="16.21", bracket="gold", queue_id=420, game_start_ms=start))
            for i in range(10):
                s.add(MatchParticipant(match_id=match_id, champ_id=100 + i, team_id=100 if i < 5 else 200,
                                       role=ROLES[i % 5], win=i < 5))
        s.commit()


def test_backfill_fills_leads_is_resumable_and_never_fetches_twice(engine, tmp_path):
    _unfetched(engine, ["EUW1_1", "EUW1_2", "EUW1_3"])
    riot = _Riot(missing=("EUW1_2",))
    stop_after_one = iter((False, True))
    result = backfill_timelines(engine, riot, tmp_path, done=lambda: next(stop_after_one))
    assert result == {"updated": 1, "left": 2} and riot.calls == ["EUW1_3"]  # newest first
    assert backfill_timelines(engine, riot, tmp_path) == {"updated": 2, "left": 0}
    assert backfill_timelines(engine, riot, tmp_path) == {"updated": 0, "left": 0}
    assert sorted(riot.calls) == ["EUW1_1", "EUW1_2", "EUW1_3"]
    with Session(engine) as s:
        assert s.get(MatchParticipant, ("EUW1_3", 100)).lane_gold_15 == -500
        assert s.get(MatchParticipant, ("EUW1_2", 100)).lane_gold_15 is None  # no timeline: done, no lead
        assert s.get(CrawledMatch, "EUW1_2").timeline_done


def test_backfill_reads_the_match_from_the_disk_cache_when_it_can(engine, tmp_path):
    from draftassist.data.riot import REGION, cache_path, match_path

    _unfetched(engine, ["EUW1_1"])
    path = cache_path(tmp_path / "riot", REGION, match_path("EUW1_1"), None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"body": _match()}), encoding="utf-8")

    class NoMatch(_Riot):
        def match(self, match_id):
            raise AssertionError("should have used the cache")

    assert backfill_timelines(engine, NoMatch(), tmp_path)["updated"] == 1


def test_timelines_are_not_written_to_the_disk_cache(tmp_path):
    import httpx

    from draftassist.data.riot import RiotClient

    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"info": {"frames": []}})

    riot = RiotClient("key", tmp_path, transport=httpx.MockTransport(respond), sleep=lambda s: None)
    riot.timeline("EUW1_9")
    riot.timeline("EUW1_9")
    assert calls == ["/lol/match/v5/matches/EUW1_9/timeline"] * 2
    assert not list(tmp_path.rglob("*.json"))


def test_crawler_stores_leads_when_asked(engine):
    from draftassist.data.crawler import Crawler
    from draftassist.db.models import SeedPlayer

    raw = {"metadata": {"matchId": "EUW1_7"}, "info": {
        "gameVersion": "16.21.1.1", "queueId": 420, "gameDuration": 1800, "gameStartTimestamp": 1, "teams": [
            {"teamId": 100, "bans": []}, {"teamId": 200, "bans": []}],
        "participants": [{**p, "win": p["teamId"] == 100} for p in _match()["info"]["participants"]]}}

    class Riot(_Riot):
        def match_ids(self, *a, **k):
            return ["EUW1_7"] if a[1] == 420 else []

        def match(self, match_id):
            return raw

    with Session(engine) as s:
        s.add(SeedPlayer(puuid="p", bracket="gold", tier="GOLD", division="I"))
        s.commit()
    Crawler(Riot(), engine, "16.21", brackets=("gold",), timelines=True).run(max_matches=1)
    with Session(engine) as s:
        assert s.get(MatchParticipant, ("EUW1_7", 100)).lane_gold_15 == -500
        assert s.get(CrawledMatch, "EUW1_7").timeline_done


# ---------- test 1: the aggregation matches a hand count ----------
def test_lane_sums_match_a_hand_count(engine):
    with Session(engine) as s:
        for c in (1, 3, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1"))
        s.commit()
        blue, red = (1, *FILLER_BLUE), (3, *FILLER_RED)
        _store(s, "EUW1_1", blue, red, True, {"top": 300})
        _store(s, "EUW1_2", blue, red, False, {"top": -100})
        _store(s, "EUW1_3", red, blue, True, {"top": 200})  # sides swapped: 3 is ahead by 200, so 1 is behind
        _store(s, "EUW1_4", blue, red, True)  # no lead recorded
        s.commit()
    aggregate(engine, "16.21")
    with Session(engine) as s:
        m = s.get(Matchup, ("16.21", "gold", "top", 1, 3))
    assert (m.games, m.lane_n, m.lane_gold_sum, m.lane_xp_sum) == (4, 3, 0.0, 0.0)  # 300 - 100 - 200


def test_lane_sums_add_up_from_the_lower_champion_id(engine):
    with Session(engine) as s:
        for c in (1, 3, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1"))
        s.commit()
        _store(s, "EUW1_1", (1, *FILLER_BLUE), (3, *FILLER_RED), True, {"top": 300})
        _store(s, "EUW1_2", (3, *FILLER_BLUE), (1, *FILLER_RED), True, {"top": 100})
        s.commit()
    aggregate(engine, "16.21")
    with Session(engine) as s:
        m = s.get(Matchup, ("16.21", "gold", "top", 1, 3))
    assert (m.lane_n, m.lane_gold_sum, m.lane_xp_sum) == (2, 200.0, 100.0)


# ---------- test 2: slopes are positive and printed ----------
def test_slopes_recover_what_a_lead_is_worth_per_archetype():
    rng = np.random.default_rng(2)
    n = 40_000
    a = rng.integers(0, 2, n)
    lead = rng.normal(0, 1.0, n)  # thousands of gold
    true = np.array([1.2, 0.4])[a]
    won = rng.random(n) < 1 / (1 + np.exp(-(true * lead)))
    got = fit_lane_slopes(a, lead, np.zeros(n), won, 2)
    assert got[0] == pytest.approx(1.2, abs=0.1) and got[1] == pytest.approx(0.4, abs=0.1)


def test_slopes_with_no_data_are_the_default():
    empty = np.array([])
    assert fit_lane_slopes(empty.astype(int), empty, empty, empty.astype(bool), 3) == pytest.approx([1.0, 1.0, 1.0])


def test_real_pipeline_stores_positive_slopes_and_prints_them(db):
    model = load_matchup_model(db)
    assert model.slope("top", "tank") > 0 and model.slope("top", "ranged") > 0
    text = format_matchup_report(db)
    assert "1,000 gold" in text and "role weight" in text and "tank" in text


# ---------- test 3: lane weight 0 and role weights 1 give the Stage 3 outputs ----------
def test_with_lane_weight_0_and_role_weights_1_outputs_are_unchanged():
    data, team = synthetic_data()
    with_lane = {b: replace(s, lane_gold={k: 300.0 for k in s.matchups}) for b, s in data.brackets.items()}
    plain = DraftEngine(data, team, EngineConfig())
    laned = DraftEngine(replace(data, brackets=with_lane), team,
                        EngineConfig(lane_weight=0.0, role_weights=dict.fromkeys(ROLES, 1.0)))
    for state in (DraftState(TOURNAMENT, "blue"), DraftState(TOURNAMENT, "blue", tuple(sorted(data.champions())[:6]))):
        step = TOURNAMENT.steps[len(state.actions)].kind
        a = plain.suggest_bans(state) if step == "ban" else plain.suggest_picks(state, k=5)
        b = laned.suggest_bans(state) if step == "ban" else laned.suggest_picks(state, k=5)
        assert [(x.champ_id, round(x.value, 9)) for x in a] == [(x.champ_id, round(x.value, 9)) for x in b]


# ---------- reading the two numbers ----------
def _stats(gold=200.0, game=0.55):
    meta = {1: {"top": RoleStat(0.5, 0.1)}, 2: {"top": RoleStat(0.5, 0.1)}}
    return BracketStats(meta, {("top", 1, 2): game}, strength_in_matchups=True, lane_gold={("top", 1, 2): gold},
                        lane_slope={(1, "top"): 1.0, (2, "top"): 0.5})


def test_lane_numbers_are_antisymmetric():
    s = _stats()
    assert s.lane_gold_of("top", 1, 2) == 200 and s.lane_gold_of("top", 2, 1) == -200
    assert s.lane_gold_of("top", 1, 3) is None
    assert s.lane_logodds("top", 1, 2) == pytest.approx(0.2 * 0.75)  # the mean of both champions' slopes
    assert s.lane_logodds("top", 2, 1) == pytest.approx(-0.2 * 0.75)
    assert s.lane_wr("top", 2, 1) == pytest.approx(1 - sigmoid(0.15))


def test_blend_and_counter_term():
    s = _stats()
    assert s.blended_wr("top", 1, 2, 0.0) == pytest.approx(0.55)
    assert s.blended_wr("top", 1, 2, 1.0) == pytest.approx(sigmoid(0.15))
    assert s.blended_wr("top", 1, 2, 0.5) == pytest.approx(0.5 * 0.55 + 0.5 * sigmoid(0.15))
    assert s.counter_term("top", 1, 2, 0.0) == pytest.approx(s.matchup_edge("top", 1, 2))
    assert s.counter_term("top", 1, 2, 1.0) == pytest.approx(0.15)
    plain = BracketStats(s.meta, s.matchups, strength_in_matchups=True)  # no lane data: the game matchup at any weight
    assert plain.blended_wr("top", 1, 2, 1.0) == pytest.approx(0.55)
    assert plain.counter_term("top", 1, 2, 1.0) == pytest.approx(plain.matchup_edge("top", 1, 2))
    assert plain.blended_wr("top", 1, 9, 0.5) is None


def _engine(stats, cfg):
    team = OurTeam.from_tiers({"top": {1: "main"}}, dict.fromkeys(ROLES, "gold"))
    return DraftEngine(EngineData({"gold": stats}, {1: "Aa", 2: "Bb"}), team, cfg)


def test_resp_reads_the_blend():
    data = EngineData({"gold": _stats()}, {1: "Aa", 2: "Bb"})
    team = OurTeam.from_tiers({"top": {1: "main"}}, dict.fromkeys(ROLES, "gold"))
    assert BracketLaneData(data, team, "gold").matchup_wr("top", 1, 2) == pytest.approx(0.55)
    assert BracketLaneData(data, team, "gold", lane_weight=1.0).matchup_wr("top", 1, 2) == pytest.approx(sigmoid(0.15))


def test_p_win_uses_the_game_matchup_and_the_role_weight():
    s = _stats()
    plain = _engine(s, EngineConfig(lane_weight=1.0))
    assert plain._terms([1], {1: "top"}, [2], {2: "top"}).lane == pytest.approx(s.matchup_edge("top", 1, 2))
    half = _engine(s, EngineConfig(role_weights={"top": 0.5}))
    assert half._terms([1], {1: "top"}, [2], {2: "top"}).lane == pytest.approx(0.5 * s.matchup_edge("top", 1, 2))


def test_team_lane_ban_scales_by_role_weight():
    row = lambda x, ban: type("R", (), {"opp": x, "ban": ban})()
    top = RoleBanTable("top", 0.0, (row(1, 2.0),), False)
    jungle = RoleBanTable("jungle", 0.0, (row(1, 1.0),), False)
    assert team_lane_ban([top, jungle]) == {1: 3.0}
    assert team_lane_ban([top, jungle], {"top": 1.5, "jungle": 0.5}) == {1: 3.5}


def test_a_lead_past_300_gold_is_always_mentioned_without_a_verdict():
    s = _stats(gold=-310.0, game=0.56)
    assert _engine(s, EngineConfig())._lane_note(s, "top", 1, 2).startswith("\u2212310 gold at 15 vs Bb, game +")
    assert "Loses" not in _engine(s, EngineConfig())._lane_note(s, "top", 1, 2)
    ahead = _stats(gold=310.0, game=0.45)
    assert _engine(ahead, EngineConfig())._lane_note(ahead, "top", 1, 2).startswith("+310 gold at 15 vs Bb, game \u2212")
    under = _stats(gold=299.0, game=0.56)
    assert _engine(under, EngineConfig())._lane_note(under, "top", 1, 2) is None
    none = BracketStats(s.meta, s.matchups, strength_in_matchups=True)
    assert _engine(none, EngineConfig())._lane_note(none, "top", 1, 2) is None


def test_the_mention_reaches_the_pick_reason_even_when_another_term_is_bigger():
    s = _stats(gold=-310.0, game=0.56)
    eng = _engine(s, EngineConfig())
    text = eng._pick_reason(1, "top", {1: "top"}, {2: "top"}, [], 0.0, 0.0, None)
    assert "\u2212310 gold at 15 vs Bb" in text
    assert "counters their Bb" in text  # the game edge is still its largest term here


# ---------- the fits ----------
def test_gold_fit_recovers_a_planted_archetype_effect():
    labels = {i: arch(("juggernaut", "diver", "skirmisher", "tank", "ranged")[i % 5]) for i in range(30)}
    feats = features(labels)
    a, b = np.triu_indices(30, 1)
    coefs = np.array([300.0 if n == "E:tank>ranged" else 0.0 for n in feats.names])
    rng = np.random.default_rng(7)
    n = np.full(len(a), 400.0)
    mean = feats.pair_features(a, b) @ coefs + rng.normal(0, 1000 / 20, len(a))
    got = fit_lane_gold(feats, a, b, n, mean, np.zeros(30), np.zeros(len(feats.names)), sigma2=1000.0 ** 2)
    assert dict(zip(feats.names, got))["E:tank>ranged"] == pytest.approx(300, abs=80)
    empty = np.array([], dtype=int)
    prior = np.arange(len(feats.names), dtype=float)
    assert fit_lane_gold(feats, empty, empty, np.array([]), np.array([]), np.zeros(30), prior, 1e6) == pytest.approx(prior)


def test_k_for_gold_follows_the_spread_beyond_noise():
    rng = np.random.default_rng(4)
    n = np.full(400, 400.0)
    sigma2 = 1000.0 ** 2
    def k(sd):
        return empirical_k_gold(n, rng.normal(0, sd, 400) + rng.normal(0, 50, 400), np.zeros(400), sigma2)
    assert k(0.0) == 400  # noise only: the ceiling
    assert 50 < k(90.0) < 400
    assert k(1000.0) == 50
    assert empirical_k_gold(np.array([]), np.array([]), np.array([]), sigma2) == 200


def test_role_weights_recover_which_roles_matter_and_average_one():
    rng = np.random.default_rng(6)
    n = 60_000
    edges = rng.normal(0, 0.3, (n, 5))
    true = np.array([1.5, 0.0, 1.0, 1.0, 1.0])
    won = rng.random(n) < 1 / (1 + np.exp(-(edges @ true)))
    weights, se = fit_role_weights(edges, np.zeros(n), won)
    assert weights.mean() == pytest.approx(1.0)
    assert weights[1] == weights.min() and weights[1] < 0.3 and weights[0] == weights.max()
    assert (se > 0).all() and (weights >= 0).all()


def test_role_weights_with_no_data_are_all_one():
    weights, _ = fit_role_weights(np.zeros((0, 5)), np.zeros(0), np.zeros(0, dtype=bool))
    assert weights == pytest.approx(np.ones(5))


def test_pipeline_finds_who_wins_lane(db):
    model = load_matchup_model(db)
    table = model.lane_table()
    assert table[("top", 1, 3)] < -100  # the ranged champion is ahead at 15
    assert len(load_role_weights(db)) == 5
    with Session(db) as s:
        assert s.exec(select(Matchup).where(Matchup.lane_n > 0)).first() is not None


def test_no_gold_yet_means_no_lane_matchups(engine):
    with Session(engine) as s:
        for c in (1, 3, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.21.1", tags=["Fighter"]))
        s.commit()
        for i in range(60):
            _store(s, f"EUW1_{i}", (1, *FILLER_BLUE), (3, *FILLER_RED), i % 2 == 0)
        s.commit()
    aggregate(engine, "16.21")
    assert load_matchup_model(engine).lane_table() == {}


# ---------- settings ----------
def test_settings_round_trip_and_validation(db):
    assert get_engine_settings(db)["lane_weight"] == 0.5
    save_engine_settings(db, lane_weight=0.3, role_weight_override={"jungle": 0.4})
    e = get_engine_settings(db)
    assert e["lane_weight"] == 0.3 and e["role_weight_override"] == {"jungle": 0.4}
    cfg = engine_config(db)
    assert cfg.lane_weight == 0.3 and cfg.role_weights["jungle"] == 0.4
    assert cfg.role_weights["top"] == pytest.approx(load_role_weights(db)["top"])  # others stay fitted
    for bad in ({"lane_weight": 1.5}, {"role_weight_override": {"jungle": -1}}, {"role_weight_override": {"bot": 1}}):
        with pytest.raises(ValueError):
            save_engine_settings(db, **bad)
    payload = settings_payload(db)
    assert set(payload["role_weights_fitted"]) == set(ROLES) and payload["lane_weight"] == 0.3
    save_engine_settings(db, role_weight_override={})
    assert get_engine_settings(db)["role_weight_override"] == {}


def test_recap_shows_lane_and_game(db):
    from draftassist.api.recap import lane_matchup

    m = lane_matchup(db, "16.21", "gold", "top", 1, 3)
    assert m["game_pp"] is not None and m["lane_gold"] < -100
    assert m["delta_pp"] < m["game_pp"]  # the blend pulls towards the lost lane
    other_way = lane_matchup(db, "16.21", "gold", "top", 3, 1)
    assert other_way["delta_pp"] == pytest.approx(-m["delta_pp"], abs=0.11)
    assert other_way["lane_gold"] == -m["lane_gold"]


def test_storing_no_timeline_marks_the_match_done(engine):
    _unfetched(engine, ["EUW1_1"])
    store_timeline(engine, "EUW1_1", None, _match())
    with Session(engine) as s:
        assert s.get(CrawledMatch, "EUW1_1").timeline_done
