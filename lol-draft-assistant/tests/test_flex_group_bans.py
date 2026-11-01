"""docs/flex-simultaneous-bans.md: 5 coordinated ban suggestions during Flex's simultaneous bans."""
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from draftassist.api.main import create_app
from draftassist.engine.engine import DraftEngine, enemy_team_ban_prob
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.model import BracketStats, EngineConfig, EngineData, OurTeam, RoleStat
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import DraftState
from draftassist.lcu.demo import demo_sessions

FLEX = Path(__file__).resolve().parents[1] / "fixtures" / "lcu" / "flex-ranked-sample"
ROLES = ("top", "jungle", "mid", "adc", "support")


def flex_event(name):
    return json.loads((FLEX / name).read_text(encoding="utf-8"))


# 1. Five coordinated suggestions with distinct owners ------------------------------------
def test_flex_ban_phase_shows_five_owned_suggestions(make_services):
    svc = make_services()
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        svc.live.handle_session(flex_event("0004-session.json"))  # ban phase, nobody has banned yet
        view = client.get("/api/live").json()
    center = view["center"]
    assert center["mode"] == "our_ban_group"
    cards = center["cards"]
    assert len(cards) == 5
    assert sorted(c["role"] for c in cards) == sorted(ROLES)  # one per teammate
    picks = [c["suggestion"]["champ"]["id"] for c in cards if c["suggestion"]]
    assert len(picks) == 5 and len(set(picks)) == 5
    assert all(c["status"] == "suggested" for c in cards)


# 2. Greedy recomputation ------------------------------------------------------------------
def group_data(top_rates=(0.20, 0.19), mid_rate=0.148):
    """Our top (P) loses to A and B (-8pp each); our mid (M) loses to C (-8pp). Everything else is
    an even matchup. All win rates 50%, so only the Matchup component differs."""
    A, B, T3, C, M3, P, M = 1, 2, 3, 4, 5, 7, 8
    a_rate, b_rate = top_rates
    meta = {A: {"top": RoleStat(0.5, a_rate)}, T3: {"top": RoleStat(0.5, 1 - a_rate - b_rate)},
            C: {"mid": RoleStat(0.5, mid_rate)}, M3: {"mid": RoleStat(0.5, 1 - mid_rate)},
            P: {"top": RoleStat(0.5, 0.0)}, M: {"mid": RoleStat(0.5, 0.0)}}
    matchups = {("top", P, A): 0.42, ("top", P, T3): 0.5, ("mid", M, C): 0.42, ("mid", M, M3): 0.5}
    if b_rate:
        meta[B] = {"top": RoleStat(0.5, b_rate)}
        matchups[("top", P, B)] = 0.42
    data = EngineData({"gold": BracketStats(meta, matchups)}, {c: f"C{c}" for c in meta})
    team = OurTeam.from_tiers({"top": {P: "main"}, "mid": {M: "main"}}, {r: "gold" for r in ROLES})
    return data, team, (A, B, C)


def test_greedy_recomputes_after_each_ban():
    """Scored once: A (1.22pp) > C (1.18) > B (1.15), so a plain top-2 would be A and C. After A is
    banned, B is worth 1.9pp: the enemy's remaining top options are now worse for us. Recomputation
    picks that up and takes B. (Under the lane ban score this is right: banning both top threats
    gains more top matchup quality than one top plus one mid ban.)"""
    data, team, (A, B, C) = group_data()
    engine = DraftEngine(data, team)
    state = DraftState(STANDARD_RANKED, "blue")
    assert [r["champ_id"] for r in engine.ban_breakdown(state)][:3] == [A, C, B]
    chosen = [s.champ_id for _, s in engine.ban_group(state, ["top", "mid"]) if s]
    assert sorted(chosen) == sorted([A, B])


def test_owners_follow_lane_contribution():
    data, team, (A, _, C) = group_data(top_rates=(0.2, 0.0), mid_rate=0.2)  # one threat per lane
    engine = DraftEngine(data, team)
    owners = {s.champ_id: owner for owner, s in engine.ban_group(DraftState(STANDARD_RANKED, "blue"), ["mid", "top"]) if s}
    assert owners == {A: "top", C: "mid"}  # each ban goes to the teammate whose lane it protects


# 3. Enemy-ban discount --------------------------------------------------------------------
def test_enemy_ban_discount():
    assert enemy_team_ban_prob(0.5) == pytest.approx(0.2929, abs=1e-4)
    assert 1 - enemy_team_ban_prob(0.5) == pytest.approx(0.7071, abs=1e-4)
    assert enemy_team_ban_prob(0.0) == 0.0
    data, t = synthetic_data()
    engine = DraftEngine(data, t)
    blind = {r["champ_id"]: r for r in engine.ban_breakdown(DraftState(STANDARD_RANKED, "blue"))}
    tournament = {r["champ_id"]: r for r in engine.ban_breakdown(DraftState(TOURNAMENT, "blue"))}
    gold = data.brackets["gold"]
    for c, r in blind.items():
        assert r["discount"] == pytest.approx(1 - enemy_team_ban_prob(gold.ban_rates.get(c, 0.0)))
    assert all(r["discount"] == 1.0 for r in tournament.values())


# 4. A teammate locks a ban mid-phase ---------------------------------------------------------
def test_locked_teammate_ban_fills_their_slot(make_services):
    svc = make_services()
    view = svc.live.view(flex_event("0007-session.json"))  # one teammate has locked champion 234
    cards = view["center"]["cards"]
    locked = [c for c in cards if c["status"] == "locked"]
    assert len(locked) == 1 and locked[0]["status_champ"]["id"] == 234 and locked[0]["suggestion"] is None
    others = [c["suggestion"]["champ"]["id"] for c in cards if c["suggestion"]]
    assert len(others) == 4 and len(set(others)) == 4 and 234 not in others


def test_hovering_teammate_keeps_their_choice_visible(make_services):
    svc = make_services()
    cards = svc.live.view(flex_event("0006-session.json"))["center"]["cards"]  # teammates hover 234 and 893
    hovering = {c["status_champ"]["id"] for c in cards if c["status"] == "hovering"}
    assert hovering == {234, 893}
    suggested = [c["suggestion"]["champ"]["id"] for c in cards if c["suggestion"]]
    assert not hovering & set(suggested)


# 5. Declared pick intents ---------------------------------------------------------------------
def test_declared_pick_intent_is_never_banned():
    data, t = synthetic_data()
    engine = DraftEngine(data, t)
    plain = DraftState(STANDARD_RANKED, "blue")
    top_ban = engine.suggest_bans(plain, k=1)[0].champ_id
    declared = DraftState(STANDARD_RANKED, "blue", hovers=((top_ban, "top"),))
    assert top_ban not in [s.champ_id for s in engine.suggest_bans(declared, k=10)]
    assert top_ban not in [s.champ_id for _, s in engine.ban_group(declared, list(ROLES)) if s]
    allowed = DraftEngine(data, t, EngineConfig(ban_declared_intents=True))
    assert top_ban in [s.champ_id for s in allowed.suggest_bans(declared, k=10)]


# 6. Tournament draft is unchanged --------------------------------------------------------------
def test_tournament_single_ban_turn_still_shows_three(make_services):
    svc = make_services()
    for event in demo_sessions(svc.engine, TOURNAMENT, "blue"):
        view = svc.live.view(event)
        mode = view["center"]["mode"]
        assert mode != "our_ban_group"
        if mode == "our_ban":
            assert 1 <= len(view["center"]["cards"]) <= 3


def test_group_speed():
    data, t = synthetic_data()
    engine = DraftEngine(data, t)
    t0 = time.perf_counter()
    chosen = engine.ban_group(DraftState(STANDARD_RANKED, "blue"), list(ROLES))
    assert time.perf_counter() - t0 < 0.300
    assert sum(1 for _, s in chosen if s) == 5
