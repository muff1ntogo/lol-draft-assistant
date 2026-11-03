"""Synergy v2, Stage 6: pin a plan into the live draft (banner, plan bonuses, History adherence).
See docs/synergy-v2.md."""
import json

import pytest
from fastapi.testclient import TestClient

from draftassist.api.main import create_app
from draftassist.db.models import Champion
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.plan import build_pin, pin_effects
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import SKIPPED, DraftState
from draftassist.engine.style import StyleModel
from draftassist.engine.synergy_v2 import SynergyEvaluator
from draftassist.engine.team import TeamModel
from draftassist.engine.traits import EMPTY_TRAITS
from draftassist.lcu.demo import demo_sessions

from conftest import REAL_NAMES


def evaluator(data):
    return SynergyEvaluator(data.synergy, EMPTY_TRAITS, StyleModel([], {}), TeamModel([], EMPTY_TRAITS))


def setup(seed=3):
    data, team = synthetic_data(seed=seed)
    engine = DraftEngine(data, team)
    plan = {}
    for r in ("jungle", "mid", "adc", "support"):
        plan[r] = next(c for c in sorted(team.pools[r]) if c not in plan.values())
    return data, team, engine, plan


# ---------- the pinned plan ----------
def test_pin_holds_orders_and_bans_for_both_sides_and_formats():
    data, team, engine, plan = setup()
    pin = build_pin(engine, evaluator(data), plan)
    assert pin["locks"] == plan
    for fmt in ("tournament", "ranked"):
        for side in ("blue", "red"):
            order = pin["orders"][fmt][side]
            assert sorted(o["champ"] for o in order) == sorted(plan.values())
            assert all(o["slot"][0] == side[0].upper() for o in order)
            assert len(pin["bans"][fmt][side]) <= 5
            assert not set(plan.values()) & set(pin["bans"][fmt][side])
    assert set(pin["backups"]) == set(plan)
    assert pin["ban_bonus"] == 0.3 and pin["pick_bonus"] == 0.3
    json.dumps(pin)  # stored as JSON


# ---------- 1. banner states ----------
def test_banner_intact_at_risk_broken():
    data, team, engine, plan = setup()
    pin = build_pin(engine, evaluator(data), plan)
    st = DraftState(TOURNAMENT, "blue")
    assert pin_effects(pin, st).banner["state"] == "intact"
    target = plan["mid"]
    risky = pin_effects(pin, st, enemy_hovers=frozenset({target})).banner
    assert risky["state"] == "at_risk" and risky["at_risk"] == [target]
    st = st.apply(SKIPPED).apply(target)  # red bans our planned mid
    broken = pin_effects(pin, st).banner
    assert broken["state"] == "broken"
    assert broken["gone"] == [{"champ": target, "role": "mid", "backup": pin["backups"]["mid"]}]
    ours = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6 + (plan["jungle"],))
    assert pin_effects(pin, ours).banner["state"] == "intact"  # our own pick of a plan champion isn't "gone"


def play_with_pin(svc, pin, side="blue", game_id=7_200_000_001):
    events = demo_sessions(svc.engine, TOURNAMENT, side, game_id=game_id)
    svc.pinned = pin
    states = []
    svc.live.handle_phase("ChampSelect", None)
    for e in events:
        view = svc.live.handle_session(e)
        states.append((view.get("plan") or {}).get("state"))
    return events, states


@pytest.fixture
def svc(make_services, engine, session):
    for cid, name in REAL_NAMES.items():
        session.add(Champion(champ_id=cid, key=name.replace("'", "").replace(" ", ""), name=name, patch="16.21.1"))
    session.commit()
    return make_services(db=engine)


def enemy_bans(events, side):
    """Champions the enemy banned, in the order the replay shows them."""
    last = events[-1]
    their = [a for t in last["actions"] for a in t if a["type"] == "ban" and not a["isAllyAction"] and a["championId"] > 0]
    return [a["championId"] for a in sorted(their, key=lambda a: a["id"])]


def test_replayed_draft_banner_changes_when_a_plan_champion_is_banned(svc):
    events = demo_sessions(svc.engine, TOURNAMENT, "blue", game_id=7_200_000_001)
    banned = enemy_bans(events, "blue")[0]
    team = svc.engine.team
    plan = {"top": banned}
    for r in ("jungle", "mid"):
        plan[r] = next(c for c in sorted(team.pools[r]) if c not in plan.values() and c not in
                       {a["championId"] for t in events[-1]["actions"] for a in t})
    pin = build_pin(svc.engine, svc.synergy_v2(), plan)
    _, states = play_with_pin(svc, pin)
    assert states[0] == "intact"
    first_broken = states.index("broken")
    assert all(s in ("intact", "at_risk") for s in states[:first_broken])
    assert all(s == "broken" for s in states[first_broken:])
    view = svc.hub.latest
    assert view["plan"]["gone"][0]["champ"]["id"] == banned
    assert "Plan:" in view["plan"]["text"]


# ---------- 2. bonuses ----------
def test_ban_bonus_only_for_plan_bans_and_guardrails_win():
    data, team, engine, plan = setup()
    st = DraftState(TOURNAMENT, "blue")
    base = engine.suggest_bans(st, k=200)
    positive = [s for s in base if s.value > 0]
    x = positive[8].champ_id  # a useful ban well outside the top 3
    bonus = {x: 1.0}
    top = engine.suggest_bans(st, k=3, bonus=bonus)
    assert top[0].champ_id == x and top[0].plan and top[0].reason == positive[8].reason
    assert not any(s.plan for s in top[1:])
    hurt = next(s.champ_id for s in engine._ban_candidates(st) if s.value <= 0)
    assert hurt not in {s.champ_id for s in engine.suggest_bans(st, k=200, bonus={hurt: 5.0})}  # still not a ban
    banned = DraftState(TOURNAMENT, "blue", (x,))
    assert x not in {s.champ_id for s in engine.suggest_bans(banned.apply(SKIPPED), k=200, bonus=bonus)}


def test_pick_bonus_only_at_the_plan_champions_slot():
    data, team, engine, plan = setup()
    pin = build_pin(engine, evaluator(data), plan)
    order = {o["champ"]: o["n"] for o in pin["orders"]["tournament"]["blue"]}
    first = min(order, key=order.get)
    later = max(order, key=order.get)
    b1 = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6)  # blue's first pick
    eff = pin_effects(pin, b1)
    assert first in eff.pick_bonus and later not in eff.pick_bonus
    picks = engine.suggest_picks(b1, k=5, bonus=eff.pick_bonus)
    flagged = [s for s in picks if s.plan]
    assert all(s.champ_id in eff.pick_bonus for s in flagged)
    assert first in {s.champ_id for s in picks}
    # guardrails still win: a due plan champion that is gone is never suggested
    gone = DraftState(TOURNAMENT, "blue", (first,) + (SKIPPED,) * 5)
    assert first not in {s.champ_id for s in engine.suggest_picks(gone, k=200, bonus=pin_effects(pin, gone).pick_bonus)}
    # no bonus on ban turns for picks, and none for champions outside the plan
    assert pin_effects(pin, DraftState(TOURNAMENT, "blue")).pick_bonus == {}


def test_live_cards_carry_the_plan_tag(svc):
    team = svc.engine.team
    plan = {}
    for r in ("jungle", "mid"):
        plan[r] = next(c for c in sorted(team.pools[r]) if c not in plan.values())
    pin = build_pin(svc.engine, svc.synergy_v2(), plan)
    events, _ = play_with_pin(svc, pin)
    seen_ban_tag = False
    for e in events:
        view = svc.live.view(e)
        center = view["center"]
        for card in center.get("cards") or []:
            if card.get("plan"):
                cid = card["champ"]["id"]
                if center["mode"] == "our_ban":
                    assert cid in pin["bans"]["tournament"]["blue"]
                    seen_ban_tag = True
                elif center["mode"] == "our_pick":
                    assert cid in plan.values()
    assert seen_ban_tag


# ---------- 3. History ----------
def test_history_shows_plan_adherence(svc):
    from test_draftlog import eog

    events = demo_sessions(svc.engine, TOURNAMENT, "red", game_id=7_200_000_002)
    final = [a for t in events[-1]["actions"] for a in t if a["completed"] and a["isAllyAction"] and a["type"] == "pick"]
    our_picks = [a["championId"] for a in final]
    team = svc.engine.team
    other = next(c for r in ("support", "adc") for c in sorted(team.pools[r]) if c not in our_picks)
    pin = build_pin(svc.engine, svc.synergy_v2(), {"top": our_picks[0], "support": other})
    play_with_pin(svc, pin, side="red", game_id=7_200_000_002)
    svc.live.handle_phase("GameStart", None)
    svc.live.handle_session(None)
    svc.live.handle_phase("InProgress", None)
    svc.live.handle_phase("EndOfGame", None)
    svc.live.handle_end_of_game(eog(7_200_000_002, "red", True))
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        row = client.get("/api/history").json()["games"][0]
        assert row["plan"] == {"followed": "partly", "picked": 1, "total": 2}
        detail = client.get(f"/api/history/{row['draft_id']}").json()
        assert detail["plan"]["followed"] == "partly"
        assert {c["id"] for c in detail["plan"]["champs"]} == {our_picks[0], other}


def test_pin_api_and_unpin(svc):
    team = svc.engine.team
    locks = {}
    for r in ("jungle", "mid"):
        locks[r] = next(c for c in sorted(team.pools[r]) if c not in locks.values())
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.get("/api/plan/pin").json() == {"pinned": None}
        body = client.post("/api/plan/pin", json={"locks": locks}).json()
        assert body["pinned"]["locks"] == {r: c for r, c in locks.items()}
        assert client.get("/api/plan/pin").json()["pinned"]["locks"] == locks
        assert svc.pinned is not None
        assert client.delete("/api/plan/pin").json() == {"pinned": None}
        assert svc.pinned is None
        assert client.post("/api/plan/pin", json={"locks": {}}).status_code == 422
    # survives a restart: stored in the database
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        client.post("/api/plan/pin", json={"locks": locks})
    svc.pinned = None
    from draftassist.api.main import load_pinned

    assert load_pinned(svc.db)["locks"] == {r: c for r, c in locks.items()}


def test_live_budgets_hold_with_a_pinned_plan():
    import time

    from draftassist.engine.formats import STANDARD_RANKED
    from draftassist.engine.model import ROLES

    data, team = synthetic_data()  # full size
    engine = DraftEngine(data, team)
    plan = {}
    for r in ("jungle", "mid", "adc", "support"):
        plan[r] = next(c for c in sorted(team.pools[r]) if c not in plan.values())
    pin = build_pin(engine, evaluator(data), plan)
    st = DraftState(TOURNAMENT, "blue")
    eff = pin_effects(pin, st)
    start = time.perf_counter()
    engine.suggest_bans(st, k=5, bonus=eff.ban_bonus)
    assert time.perf_counter() - start < 0.2
    flex = DraftState(STANDARD_RANKED, "blue")
    eff = pin_effects(pin, flex)
    start = time.perf_counter()
    engine.ban_group(flex, list(ROLES), bonus=eff.ban_bonus)
    assert time.perf_counter() - start < 0.3
    b1 = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 6)
    eff = pin_effects(pin, b1)
    start = time.perf_counter()
    engine.suggest_picks(b1, k=5, bonus=eff.pick_bonus)
    assert time.perf_counter() - start < 0.2


# ---------- the plan bar (bottom of the Live screen) ----------
def _live(st, our_slots=()):
    from draftassist.lcu.parser import LiveDraft

    return LiveDraft(st, tuple(our_slots), (), "BAN_PICK", 20.0, None, None, 1)


def test_plan_bar_shows_what_differs_from_the_plan():
    from draftassist.api.live import build_live_view
    from draftassist.data.provider import ChampionCatalog
    from draftassist.engine.formats import TOURNAMENT
    from draftassist.lcu.parser import Slot

    data, team, engine, plan = setup()
    pin = build_pin(engine, evaluator(data), plan)
    catalog = ChampionCatalog("16.21.1", {c: (data.name(c), data.name(c)) for c in data.champions()})
    plan_bans = pin["bans"]["tournament"]["blue"]
    off_plan_ban = next(c for c in sorted(data.champions()) if c not in plan_bans and c not in plan.values()
                        and c not in team.pool_champs())
    # B ban (off plan), R ban (a plan ban), B ban, R ban (our planned support), B ban, R ban, then B1 picks
    # someone else for our planned jungle
    other_jg = next(c for c in sorted(team.pools["jungle"]) if c != plan["jungle"] and c not in plan.values())
    actions = (off_plan_ban, plan_bans[0], SKIPPED, plan["support"], SKIPPED, SKIPPED, other_jg)
    st = DraftState(TOURNAMENT, "blue", actions)
    view = build_live_view(engine, catalog, _live(st, [Slot(1, "jungle", other_jg, 0)]), "fixture", None, pin)
    bar = view["plan"]
    slots = {s["role"]: s for s in bar["slots"]}
    assert slots["jungle"]["status"] == "replaced" and slots["jungle"]["actual"]["id"] == other_jg
    assert slots["support"]["status"] == "banned" and slots["support"]["backup"] is not None
    assert slots["mid"]["status"] == "planned"
    assert all(s["planned"]["icon"] is not None or s["planned"]["name"] for s in bar["slots"])
    bans = {b["champ"]["id"]: b["status"] for b in bar["bans"]["plan"]}
    assert bans[plan_bans[0]] == "theirs"
    assert all(v == ("picked" if c == other_jg else "open") for c, v in bans.items() if c != plan_bans[0])
    assert [c["id"] for c in bar["bans"]["off_plan"]] == [off_plan_ban]
    for key in ("plan", "now"):
        summ = bar["summary"][key]
        assert {"damage", "timing_label", "style_label", "shares"} <= set(summ)
        if summ["damage"]:
            assert sum(summ["damage"].values()) == pytest.approx(1.0, abs=1e-3)
    assert bar["summary"]["now"]["picks"] >= 1


def test_plan_bar_while_waiting(svc):
    team = svc.engine.team
    plan = {}
    for r in ("jungle", "mid"):
        plan[r] = next(c for c in sorted(team.pools[r]) if c not in plan.values())
    svc.pinned = build_pin(svc.engine, svc.synergy_v2(), plan)
    waiting = svc.live.handle_session(None)
    assert waiting["status"] == "waiting"
    bar = waiting["plan"]
    assert [s["status"] for s in bar["slots"]] == ["planned", "planned"]
    assert bar["summary"]["now"] is None and bar["summary"]["plan"]["picks"] == 2
    svc.pinned = None
    assert svc.live.handle_session(None).get("plan") is None
