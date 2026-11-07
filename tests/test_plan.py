"""Synergy v2, Stage 5: plan-driven bans and picks (the Prep page's bottom half). See docs/synergy-v2.md."""
import random
from dataclasses import replace

import pytest

from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.model import ROLES, BracketStats
from draftassist.engine.plan import (
    PlanContext,
    backups,
    combined_bans,
    enemy_presence,
    fills,
    our_slots,
    pick_order,
    plan_risks,
    win_lane_bans,
    win_teamfight_bans,
)
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import SKIPPED, DraftState
from draftassist.engine.style import StyleModel
from draftassist.engine.synergy_v2 import SynergyEvaluator
from draftassist.engine.team import TeamModel, parse_team_rules
from draftassist.engine.traits import TAGS, ChampionTraits, TagEntry

TEAM_RULES = [
    {"id": "has_engage", "feature": {"max": "engage"}, "prior_pp": 1.5, "note": "A reliable engage"},
    {"id": "engage_vs_disengage", "ours": {"max": "engage"}, "theirs": {"max": "disengage"}, "prior_pp": -3.0,
     "note": "Disengage vs your engage comp"},
]


def tags(**v):
    return TagEntry({**dict.fromkeys(TAGS, 0), **v}, reviewed=True)


def base():
    data, team = synthetic_data(seed=3)
    return data, team


def plan_of(team, roles=("jungle", "adc", "support")):
    plan = {}
    for r in roles:
        plan[r] = next(c for c in sorted(team.pools[r]) if c not in plan.values())
    return plan


def evaluator(data, plan, special=None):
    """Every champion tagged with zeros in each of its roles; plan champions engage 3; `special` overrides."""
    t = {}
    for c, roles in ((c, [r for r in ROLES if data.brackets["gold"].role_stat(c, r)]) for c in data.champions()):
        for r in roles:
            t[(c, r)] = tags()
    for r, c in plan.items():
        t[(c, r)] = tags(engage=3)
    t |= special or {}
    traits = ChampionTraits({}, t, {})
    return SynergyEvaluator(data.synergy, traits, StyleModel([], {}), TeamModel(parse_team_rules(TEAM_RULES), traits))


def ctx(engine, ev, plan, side="blue", fmt=TOURNAMENT, state=None):
    return PlanContext(engine, ev, plan, state or DraftState(fmt, side))


def top_pick_rate(data, role, exclude=()):
    meta = data.brackets["gold"].meta
    return max((c for c in meta if role in meta[c] and c not in exclude), key=lambda c: meta[c][role].pick_rate)


# ---------- 1. Win lane ----------
def test_win_lane_uses_only_the_plan_champion_and_bans_its_worst_common_lane():
    data, team = base()
    plan = plan_of(team)
    adc = plan["adc"]
    x = top_pick_rate(data, "adc", exclude=[adc])
    brackets = dict(data.brackets)
    for b in ("gold", team.brackets["adc"]):
        bs = brackets[b]
        m = dict(bs.matchups)
        m.pop(("adc", x, adc), None)
        m[("adc", adc, x)] = 0.38  # x is the plan ADC's worst common lane: -12pp
        brackets[b] = BracketStats(bs.meta, m, bs.ban_rates)
    data = replace(data, brackets=brackets)
    engine = DraftEngine(data, team)
    c = ctx(engine, evaluator(data, plan), plan)
    tables = c.lane_tables()
    assert {row.via for row in tables["adc"].rows if row.via is not None} == {adc}  # Resp: the plan ADC only
    bans = win_lane_bans(c)
    assert bans[0].champ_id == x
    assert bans[0].reason.startswith(f"Protects {data.name(adc)}")
    assert "−12.0pp" in bans[0].reason
    assert all(b.value > 0 for b in bans) and len(bans) <= 5
    assert not set(plan.values()) & {b.champ_id for b in bans}  # never ban our own plan


# ---------- 2. Win teamfights ----------
def test_win_teamfights_ranks_a_planted_disengage_champion_first():
    data, team = base()
    plan = plan_of(team)
    d = top_pick_rate(data, "support", exclude=plan.values())
    ev = evaluator(data, plan, {(d, "support"): tags(disengage=3)})
    c = ctx(DraftEngine(data, team), ev, plan)
    bans = win_teamfight_bans(c)
    assert bans[0].champ_id == d
    assert "Disengage vs your engage comp" in bans[0].reason and "pp" in bans[0].reason
    assert enemy_presence(c.engine, d) > 0


def test_combined_adds_teamfights_to_ban_value():
    data, team = base()
    plan = plan_of(team)
    d = top_pick_rate(data, "support", exclude=plan.values())
    ev = evaluator(data, plan, {(d, "support"): tags(disengage=3)})
    c = ctx(DraftEngine(data, team), ev, plan)
    with_tf = {b.champ_id: b.value for b in combined_bans(c, d_weight=1.0, k=50)}
    without = {b.champ_id: b.value for b in combined_bans(c, d_weight=0.0, k=50)}
    assert with_tf[d] > without.get(d, 0.0)
    assert not set(plan.values()) & set(with_tf)


# ---------- 3. Pick order ----------
@pytest.mark.parametrize("side", ["blue", "red"])
def test_pick_order_contested_early_countered_late(side):
    data, team = base()
    plan = plan_of(team, ("jungle", "mid"))
    contested, countered = plan["jungle"], plan["mid"]
    brackets = {}
    for name, bs in data.brackets.items():
        bans = dict(bs.ban_rates) | {contested: 0.45, countered: 0.0}
        m = {k: v for k, v in bs.matchups.items() if k[0] != "jungle" or contested not in k[1:]}
        m = {k: v for k, v in m.items() if k[0] != "mid" or countered not in k[1:]}
        for e in [c for c in bs.meta if "mid" in bs.meta[c] and c != countered]:
            m[("mid", e, countered)] = 0.60  # everyone counters our mid
        brackets[name] = BracketStats(bs.meta, m, bans)
    data = replace(data, brackets=brackets)
    c = ctx(DraftEngine(data, team), evaluator(data, plan), plan, side=side)
    order = pick_order(c)
    slot = {o.champ: o.slot_index for o in order}
    assert slot[contested] < slot[countered]
    by = {o.champ: o for o in order}
    assert by[contested].contest > by[countered].contest
    assert by[countered].counter_edge_pp > by[contested].counter_edge_pp
    labels = [s.label for s in our_slots(TOURNAMENT, side)]
    assert labels == ([f"B{i}" for i in range(1, 6)] if side == "blue" else [f"R{i}" for i in range(1, 6)])
    assert all(o.line for o in order)


def test_slot_groups_follow_the_format():
    groups = lambda f, s: [g for g in dict.fromkeys(x.group for x in our_slots(f, s))]
    assert groups(TOURNAMENT, "blue") == ["B1", "B2–B3", "B4–B5"]
    assert groups(TOURNAMENT, "red") == ["R1–R2", "R3", "R4–R5"]
    assert groups(STANDARD_RANKED, "blue") == ["B1", "B2–B3", "B4–B5"]
    assert groups(STANDARD_RANKED, "red") == ["R1–R2", "R3–R4", "R5"]


# ---------- fills and risks ----------
def test_fills_cover_the_other_roles_with_a_breakdown():
    data, team = base()
    plan = plan_of(team)
    c = ctx(DraftEngine(data, team), evaluator(data, plan), plan)
    out = fills(c, k=3)
    assert set(out) == {"top", "mid"}
    for role, rows in out.items():
        assert 1 <= len(rows) <= 3
        assert all(s.champ in team.pools[role] for s in rows)


def test_plan_risks_name_enemy_champions_and_identities():
    data, team = base()
    plan = plan_of(team)
    d = top_pick_rate(data, "support", exclude=plan.values())
    c = ctx(DraftEngine(data, team), evaluator(data, plan, {(d, "support"): tags(disengage=3)}), plan)
    risks = plan_risks(c)
    assert risks["champions"][0]["champ"] == d and risks["champions"][0]["delta"] > 0
    assert len(risks["champions"]) <= 3 and len(risks["identities"]) <= 3
    assert all({"identity", "delta", "champs"} <= set(i) for i in risks["identities"])


# ---------- 4. Backups ----------
def test_backups_come_from_the_right_pool_and_skip_unavailable():
    data, team = base()
    plan = plan_of(team)
    sup_pool = sorted(team.pools["support"])
    banned = sup_pool[1]
    state = DraftState(TOURNAMENT, "blue", (banned,))
    c = ctx(DraftEngine(data, team), evaluator(data, plan), plan, state=state)
    out = backups(c)
    assert set(out) == set(plan)
    for role, b in out.items():
        if b is None:
            continue
        assert b.champ in team.pools[role] and b.champ != plan[role]
        assert b.champ not in state.unavailable() and b.champ not in plan.values()
        assert b.drop == pytest.approx(c.ev.evaluate(plan).overall - c.ev.evaluate({**plan, role: b.champ}).overall)
    assert out["support"].champ != banned


# ---------- 5. guardrails with plans active ----------
def test_guardrail_fuzz_with_plans():
    data, team = synthetic_data(seed=11, n_champs=60)
    engine = DraftEngine(data, team)
    champs = sorted(data.champions())
    rng = random.Random(77)
    for i in range(120):
        fmt = rng.choice([TOURNAMENT, STANDARD_RANKED])
        state = DraftState(fmt, rng.choice(["blue", "red"]))
        for _ in range(rng.randint(0, 14)):
            if state.step.kind == "ban" and rng.random() < 0.15:
                state = state.apply(SKIPPED)
            else:
                state = state.apply(rng.choice([c for c in champs if c not in state.unavailable()]))
        roles = rng.sample(ROLES, rng.randint(1, 4))
        plan = {}
        for r in roles:
            options = [c for c in team.pools[r] if c not in state.unavailable() and c not in plan.values()]
            if options:
                plan[r] = rng.choice(options)
        c = ctx(engine, evaluator(data, plan), plan, state=state)
        unavailable = state.unavailable()
        for bans in (win_lane_bans(c), win_teamfight_bans(c), combined_bans(c, 1.0)):
            got = [b.champ_id for b in bans]
            where = f"case {i}: {state.actions} plan {plan}"
            assert len(got) == len(set(got)) and len(got) <= 5, where
            assert not unavailable & set(got), where
            assert not set(plan.values()) & set(got), where
            assert all(b.value > 0 and b.relevance >= engine.cfg.min_ban_relevance for b in bans), where
        for rows in fills(c).values():
            assert not unavailable & {s.champ for s in rows}
        for b in backups(c).values():
            assert b is None or b.champ not in unavailable


def test_flex_uses_group_bans_with_owners():
    data, team = base()
    plan = plan_of(team)
    c = ctx(DraftEngine(data, team), evaluator(data, plan), plan, fmt=STANDARD_RANKED)
    bans = combined_bans(c, 1.0)
    assert 1 <= len(bans) <= 5
    assert len({b.champ_id for b in bans}) == len(bans)
    assert all(b.discount is not None and b.discount <= 1 for b in bans)


# ---------- API ----------
def test_plan_api(make_services):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    svc = make_services()
    team = svc.engine.team
    locks = {}
    for r in ("jungle", "mid", "adc", "support"):
        locks[r] = next(c for c in sorted(team.pools[r]) if c not in locks.values())
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        body = client.post("/api/prep/plan", json={"locks": locks, "side": "red", "format": "tournament"}).json()
        assert set(body["bans"]) == {"lane", "teamfight", "combined"}
        for rows in body["bans"].values():
            assert len(rows) <= 5
            assert all({"champ", "value_pp", "reason"} <= set(r) for r in rows)
        assert [o["slot"][0] for o in body["picks"]["order"]] == ["R"] * 4
        assert set(body["picks"]["fill"]) == {"top"}
        assert set(body["picks"]["backups"]) == set(locks)
        assert {"champions", "identities"} <= set(body["picks"]["risks"])
        flex = client.post("/api/prep/plan", json={"locks": locks, "side": "blue", "format": "flex"}).json()
        assert all(r.get("owner") for r in flex["bans"]["combined"])
        assert client.post("/api/prep/plan", json={"locks": locks, "side": "green"}).status_code == 422
        assert client.post("/api/prep/plan", json={"locks": {}, "side": "blue"}).json()["empty"] is True
