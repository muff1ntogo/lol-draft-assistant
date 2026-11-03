"""Synergy v2, Stage 4: the merged Prep page, top half (visualiser, plan panel, synergy table).
See docs/synergy-v2.md."""
import pytest
from fastapi.testclient import TestClient

from draftassist.api.main import create_app
from draftassist.api.prep_view import build_prep_view
from draftassist.api.synergy import draft_context
from draftassist.data.provider import ChampionCatalog
from draftassist.engine.formats import get_format
from draftassist.engine.model import OurTeam
from draftassist.engine.state import DraftState
from draftassist.engine.style import StyleModel, parse_style_rules
from draftassist.engine.synergy import PairScore, SynergyTable
from draftassist.engine.synergy_v2 import SynergyEvaluator
from draftassist.engine.team import TeamModel, parse_team_rules
from draftassist.engine.traits import TAGS, ChampionTraits, TagEntry
from draftassist.lcu.parser import LiveDraft, Slot

PP = 0.04
TOP = {122: "Darius", 516: "Ornn"}
JG = {64: "Lee Sin", 59: "Jarvan IV", 254: "Vi"}
MID = {103: "Ahri", 61: "Orianna", 268: "Azir"}
ADC = {222: "Jinx", 51: "Caitlyn"}
SUP = {89: "Leona", 117: "Lulu", 497: "Rakan"}
NAMES = TOP | JG | MID | ADC | SUP
CATALOG = ChampionCatalog("16.21.1", {c: (n.replace(" ", ""), n) for c, n in NAMES.items()})
TEAM = OurTeam.from_tiers({"top": {122: "main", 516: "comfortable"}, "jungle": {64: "main", 59: "comfortable", 254: "learning"},
                           "mid": {103: "main", 61: "comfortable", 268: "learning"}, "adc": {222: "main", 51: "comfortable"},
                           "support": {89: "main", 117: "comfortable", 497: "learning"}},
                          {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")})
ROLE_OF = {c: r for r, d in (("top", TOP), ("jungle", JG), ("mid", MID), ("adc", ADC), ("support", SUP)) for c in d}


def tags(**v):
    return TagEntry({**dict.fromkeys(TAGS, 0), **v}, reviewed=True)


def evaluator() -> SynergyEvaluator:
    def score(pp, games=500):
        return PairScore(pp * PP, pp * PP, games, games < 30)

    pairs = {(("jungle", 64), ("mid", 103)): score(3.0), (("adc", 222), ("support", 117)): score(1.0, games=40),
             (("jungle", 59), ("top", 516)): score(2.0), (("mid", 61), ("support", 89)): score(-2.0),
             (("jungle", 254), ("mid", 268)): score(-1.0, games=12)}
    keyed = {(a, b) if a < b else (b, a): v for (a, b), v in pairs.items()}
    t = {(c, ROLE_OF[c]): tags() for c in NAMES} | {
        (117, "support"): tags(enchanter=3), (222, "adc"): tags(hypercarry=3), (89, "support"): tags(engage=3),
        (59, "jungle"): tags(engage=3), (516, "top"): tags(engage=3), (64, "jungle"): tags(pick=2),
        (103, "mid"): tags(pick=3)}
    traits = ChampionTraits({(c, ROLE_OF[c]): {"frontline_all": 0.9 if c in (516, 59, 89) else 0.1, "games": 100,
                                               "dmg_phys": 0.6, "dmg_magic": 0.4, "dmg_true": 0.0} for c in NAMES}, t, {})
    style = StyleModel(parse_style_rules([
        {"pair": "ADC-SUP", "when": {"SUP": "enchanter", "ADC": "hypercarry"}, "prior_pp": 2.0, "note": "Ench + carry"},
        {"pair": "JG-MID", "when": {"MID": "pick", "JG": "pick"}, "prior_pp": 1.0, "note": "Catch duo"},
        {"pair": "TOP-JG", "when": {"TOP": "engage", "JG": "engage"}, "prior_pp": 0.8, "note": "Layered engage"}]), {})
    team = TeamModel(parse_team_rules([
        {"id": "has_engage", "feature": {"max": "engage"}, "prior_pp": 1.5, "note": "A reliable engage"},
        {"id": "no_frontline", "feature": {"lack": "frontline_all", "need": 1.5}, "prior_pp": -2.0, "note": "No front"}]),
        traits)
    return SynergyEvaluator(SynergyTable("16.21", "all", keyed, {}), traits, style, team)


def view(**kw):
    args = dict(scope="teamfight", view="overall", threshold_pp=0.0)
    return build_prep_view(evaluator(), CATALOG, TEAM, **(args | kw))


def col(v, role):
    return next(c for c in v["columns"] if c["role"] == role)


def edge(v, a, b):
    return next(e for e in v["edges"] if {e["a"]["id"], e["b"]["id"]} == {a, b})


# ---------- scopes and views ----------
def test_five_role_columns_from_pools():
    v = view()
    assert [c["role"] for c in v["columns"]] == ["top", "jungle", "mid", "adc", "support"]
    assert [n["id"] for n in col(v, "jungle")["nodes"]] == [64, 59, 254]  # main, comfortable, learning
    assert col(v, "jungle")["nodes"][0]["tier"] == "main"


def test_in_lane_scope_has_only_core_pair_edges():
    v = view(scope="lane")
    pairs = {tuple(sorted((e["a"]["role"], e["b"]["role"]))) for e in v["edges"]}
    assert pairs == {("jungle", "top"), ("jungle", "mid"), ("adc", "support")}
    assert all(e["core"] for e in v["edges"])
    e = edge(v, 64, 103)
    for k in ("stat_pp", "style_pp", "lane_pp", "overall_pp", "games", "rules", "value_pp"):
        assert k in e
    assert e["value_pp"] == e["lane_pp"]  # in-lane scope, Overall view: the pair's Lane score


def test_teamfight_scope_has_every_cross_role_edge():
    v = view(scope="teamfight")
    assert len(v["edges"]) == 2 * 3 + 2 * 3 + 2 * 2 + 2 * 3 + 3 * 3 + 3 * 2 + 3 * 3 + 3 * 2 + 3 * 3 + 2 * 3
    other = edge(v, 122, 222)
    assert not other["core"] and other["style_pp"] == 0.0 and other["lane_pp"] is None
    assert edge(v, 64, 103)["value_pp"] == edge(v, 64, 103)["overall_pp"]  # teamfight: the part of Overall


@pytest.mark.parametrize("view_name,field", [("overall", "overall_pp"), ("stat", "stat_pp"), ("style", "style_pp")])
def test_views_recolour_edges(view_name, field):
    v = view(view=view_name)
    assert v["view"] == view_name
    for e in v["edges"]:
        assert e["value_pp"] == e[field]


def test_team_view_has_no_edges_and_threshold_filters():
    assert view(view="team")["edges"] == []
    all_edges = view(threshold_pp=0.0)["edges"]
    some = view(threshold_pp=0.5)["edges"]
    assert 0 < len(some) < len(all_edges)
    assert all(abs(e["value_pp"]) >= 0.5 for e in some)


def test_hover_details_carry_all_components_and_rules():
    e = edge(view(), 222, 117)
    assert e["stat_pp"] == pytest.approx(1.0) and e["games"] == 40
    assert e["style_pp"] == pytest.approx(2.0)
    assert e["rules"][0]["label"] == "Ench + carry +2.0"


# ---------- locking: plan panel and chips ----------
def test_locking_collapses_columns_and_updates_the_plan_panel():
    ev = evaluator()
    v = build_prep_view(ev, CATALOG, TEAM, scope="teamfight", view="overall", threshold_pp=0.0,
                        locks={"jungle": 64, "mid": 103})
    assert [n["id"] for n in col(v, "jungle")["nodes"]] == [64] and col(v, "jungle")["nodes"][0]["locked"]
    plan = v["plan"]
    assert [c["id"] for c in plan["champs"]] == [64, 103]
    expected = ev.evaluate({"jungle": 64, "mid": 103})
    assert plan["team"]["overall_pp"] == pytest.approx(25 * expected.overall, abs=0.01)
    assert plan["team"]["show_team"] is False  # Team appears from 3 locked roles
    assert [p["roles"] for p in plan["pairs"]] == [["jungle", "mid"]]
    # every other node: a chip with its Overall gain if added
    leona = next(n for n in col(v, "support")["nodes"] if n["id"] == 89)
    gain = ev.evaluate({"jungle": 64, "mid": 103, "support": 89}).overall - expected.overall
    assert leona["gain_pp"] == pytest.approx(25 * gain, abs=0.01)
    assert col(v, "jungle")["nodes"][0]["gain_pp"] is None  # locked nodes have no chip
    three = build_prep_view(ev, CATALOG, TEAM, scope="teamfight", view="overall", threshold_pp=0.0,
                            locks={"jungle": 64, "mid": 103, "support": 89})
    assert three["plan"]["team"]["show_team"] is True


def test_no_locks_no_chips():
    assert all(n["gain_pp"] is None for c in view()["columns"] for n in c["nodes"])


def test_unavailable_champions_are_greyed_and_get_no_chip():
    v = view(locks={"jungle": 64}, unavailable={103})
    ahri = next(n for n in col(v, "mid")["nodes"] if n["id"] == 103)
    assert ahri["unavailable"] and ahri["gain_pp"] is None
    assert all(r["a"]["id"] != 103 and r["b"]["id"] != 103 for r in v["table"]["pairs"])


# ---------- synergy table ----------
def test_table_rows_are_core_pairs_with_components():
    v = view()
    rows = v["table"]["pairs"]
    assert len(rows) == 2 * 3 + 3 * 3 + 2 * 3  # TOP-JG, JG-MID, ADC-SUP from the pools
    r = next(x for x in rows if {x["a"]["id"], x["b"]["id"]} == {222, 117})
    assert {"stat_pp", "style_pp", "overall_pp", "games", "why"} <= set(r)
    assert r["why"] == "Ench + carry +2.0"
    assert v["table"]["completions"] == []  # fewer than 2 locks


def test_table_lists_plan_completions_from_two_locks():
    v = view(locks={"jungle": 64, "mid": 103})
    comps = v["table"]["completions"]
    assert comps and all(c["role"] in ("top", "adc", "support") for c in comps)
    gains = [c["gain_pp"] for c in comps]
    assert gains == sorted(gains, reverse=True)
    assert {"team_pp", "overall_pp", "stat_pp", "style_pp"} <= set(comps[0])


# ---------- current draft ----------
def live_draft(actions, our_slots):
    st = DraftState(get_format("tournament"), "blue", tuple(actions))
    return LiveDraft(st, tuple(our_slots), (), "BAN_PICK", 20.0, None, None, 1)


def test_draft_context_locks_and_greys():
    live = live_draft([103, 0, 0, 0, 0, 0, 64, 89], [Slot(1, "jungle", 64, 0)])
    unavailable, locks = draft_context(live, {})
    assert locks == {"jungle": 64} and unavailable == {103, 89}
    live = live_draft([0, 0, 0, 0, 0, 0, 64], [Slot(1, None, 64, 0)])
    assert draft_context(live, {64: "jungle"})[1] == {"jungle": 64}


# ---------- API ----------
@pytest.fixture
def client(make_services):
    svc = make_services()
    svc.synergy_v2_override = evaluator()
    svc.live.engine = type(svc.engine)(svc.engine.data, TEAM, svc.engine.cfg)
    svc.live.catalog = CATALOG
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        yield c, svc


def test_api_prep_synergy(client):
    c, svc = client
    body = c.post("/api/prep/synergy", json={"scope": "lane", "view": "style", "threshold_pp": 0,
                                             "locks": {"adc": 222}}).json()
    assert body["scope"] == "lane" and body["view"] == "style"
    assert [n["id"] for n in col(body, "adc")["nodes"]] == [222]
    assert body["plan"]["champs"][0]["id"] == 222
    assert body["champions"] and body["low_data_games"] == 30
    assert c.post("/api/prep/synergy", json={"scope": "everywhere"}).status_code == 422
    assert c.post("/api/prep/synergy", json={"view": "vibes"}).status_code == 422
    assert c.post("/api/prep/synergy", json={"locks": {"feeder": 1}}).status_code == 422
    svc.live.current = live_draft([103, 0, 0, 0, 0, 0, 64, 89], [Slot(1, "jungle", 64, 0)])
    body = c.post("/api/prep/synergy", json={"use_draft": True}).json()
    assert body["draft"]["live"] is True
    assert [n["id"] for n in col(body, "jungle")["nodes"]] == [64]
    assert next(n for n in col(body, "mid")["nodes"] if n["id"] == 103)["unavailable"]
