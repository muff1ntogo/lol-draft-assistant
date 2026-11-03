"""Synergy visualiser, Stage B: the Synergy page's API (nodes, pair edges, trios, locks, current draft).
See docs/synergy-visualizer.md."""
import pytest
from fastapi.testclient import TestClient

from draftassist.api.main import create_app
from draftassist.api.synergy import SynergySource, build_synergy_view, default_columns, draft_context
from draftassist.data.provider import ChampionCatalog
from draftassist.engine.formats import get_format
from draftassist.engine.model import OurTeam
from draftassist.engine.state import DraftState
from draftassist.engine.synergy import PairScore, SynergyTable
from draftassist.lcu.parser import LiveDraft, Slot

PP = 0.04  # 1pp in log-odds
JG = {64: "Lee Sin", 59: "Jarvan IV", 254: "Vi"}
MID = {103: "Ahri", 61: "Orianna", 268: "Azir"}
SUP = {89: "Leona", 111: "Nautilus", 497: "Rakan"}
NAMES = JG | MID | SUP | {122: "Darius", 222: "Jinx"}


def score(pp, games=500):
    s = pp * PP
    return PairScore(s, s, games, games < 30)


def table() -> SynergyTable:
    pairs = {
        # Lee Sin pairs very well with Ahri and Leona, and Ahri + Leona is fine: a strong trio
        (("jungle", 64), ("mid", 103)): score(3.0),
        (("jungle", 64), ("support", 89)): score(2.0),
        (("mid", 103), ("support", 89)): score(1.0),
        # Jarvan + Orianna is strong but Orianna + Nautilus is bad: passes +1pp, hides a weak pair
        (("jungle", 59), ("mid", 61)): score(4.0),
        (("jungle", 59), ("support", 111)): score(2.0),
        (("mid", 61), ("support", 111)): score(-2.0),
        # cross pairs that keep every other trio under +1pp (pairs not listed score 0)
        (("jungle", 64), ("support", 111)): score(-1.0),
        (("jungle", 59), ("support", 89)): score(-2.0),
        (("mid", 103), ("support", 497)): score(-1.0),
        (("mid", 61), ("support", 497)): score(-2.0),
        (("jungle", 254), ("mid", 268)): score(-1.0, games=12),  # low data
        (("jungle", 64), ("mid", 268)): score(0.2),  # below the 0.5pp edge cut-off
    }
    keyed = {(a, b) if a < b else (b, a): v for (a, b), v in pairs.items()}
    return SynergyTable("16.21", "all", keyed, {})


def source() -> SynergySource:
    damage = {(64, "jungle"): (0.8, 0.1, 0.1), (103, "mid"): (0.05, 0.75, 0.2), (89, "support"): (0.2, 0.75, 0.05)}
    return SynergySource(table(), damage)


CATALOG = ChampionCatalog("16.21.1", {c: (n.replace(" ", ""), n) for c, n in NAMES.items()})
TEAM = OurTeam.from_tiers({"jungle": {64: "main", 59: "comfortable", 254: "learning"},
                           "mid": {103: "main", 61: "comfortable", 268: "learning"},
                           "support": {89: "main", 111: "comfortable", 497: "learning"},
                           "top": {122: "main"}, "adc": {222: "main"}},
                          {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")})
ROLES3 = ["jungle", "mid", "support"]
PLAYERS = {"jungle": "Jung#EUW", "mid": "Midd#EUW", "support": "Supp#EUW"}


def view(**kw):
    args = dict(roles=ROLES3, threshold_pp=1.0, players=PLAYERS)
    return build_synergy_view(source(), CATALOG, TEAM, **(args | kw))


def nodes(v, role):
    return next(c for c in v["columns"] if c["role"] == role)["nodes"]


def trio_ids(v):
    return [tuple(x["id"] for x in t["champs"]) for t in v["trios"]]


# ---------- 1. nodes, edges, trios, threshold ----------
def test_columns_default_to_pools_with_tiers():
    cols = default_columns(TEAM, ROLES3)
    assert cols == {"jungle": [64, 59, 254], "mid": [103, 61, 268], "support": [89, 111, 497]}
    v = view()
    assert [c["role"] for c in v["columns"]] == ROLES3
    lee = nodes(v, "jungle")[0]
    assert (lee["id"], lee["name"], lee["tier"]) == (64, "Lee Sin", "main")
    assert lee["icon"].endswith("/LeeSin.png")
    assert v["columns"][0]["player"] == "Jung#EUW"


def test_pool_column_capped_at_15():
    big = OurTeam.from_tiers({"mid": {c: "comfortable" for c in range(1000, 1030)}}, {"mid": "gold"})
    assert len(default_columns(big, ["mid"])["mid"]) == 15


def test_edges_between_columns_only_with_sign_and_low_data():
    v = view()
    edges = {(e["a"]["id"], e["b"]["id"]): e for e in v["edges"]}
    assert len(v["edges"]) == 27  # 3 x 3 nodes, three column pairs
    e = edges.get((64, 103)) or edges.get((103, 64))
    assert e["pp"] == pytest.approx(3.0) and not e["low_data"]
    low = edges.get((254, 268)) or edges.get((268, 254))
    assert low["pp"] == pytest.approx(-1.0) and low["low_data"]
    assert all(e["a"]["role"] != e["b"]["role"] for e in v["edges"])


def test_trios_respect_threshold_and_flag_weak_pairs():
    v = view(threshold_pp=1.0)
    trios = {tuple(x["id"] for x in t["champs"]): t for t in v["trios"]}
    assert set(trios) == {(64, 103, 89), (59, 61, 111)}
    lee = trios[(64, 103, 89)]
    assert lee["pp"] == pytest.approx(2.0) and lee["weakest_pp"] == pytest.approx(1.0) and not lee["weak_pair"]
    assert lee["players"] == ["Jung", "Midd", "Supp"]
    jarvan = trios[(59, 61, 111)]
    assert jarvan["pp"] == pytest.approx(4 / 3, abs=1e-3) and jarvan["weak_pair"]
    assert trio_ids(v)[0] == (64, 103, 89)  # best first
    assert trio_ids(view(threshold_pp=1.5)) == [(64, 103, 89)]
    assert view(threshold_pp=2.5)["trios"] == []
    assert view(roles=["jungle", "mid"])["trios"] == []  # trios need three columns


def test_hand_edited_columns():
    v = view(columns={"jungle": [64, 122], "mid": [103], "support": [89]})
    darius = nodes(v, "jungle")[1]
    assert darius["id"] == 122 and darius["tier"] is None  # not in the jungler's pool
    assert [n["id"] for n in nodes(v, "mid")] == [103]


# ---------- 2. locking ----------
def test_locking_collapses_the_column_and_adds_chips():
    v = view(locks={"jungle": 64})
    assert [n["id"] for n in nodes(v, "jungle")] == [64]
    assert nodes(v, "jungle")[0]["locked"]
    for role in ("mid", "support"):
        for n in nodes(v, role):
            assert [c["role"] for c in n["chips"]] == ["jungle"]
            assert n["chips"][0]["label"] == "J"
    ahri = next(n for n in nodes(v, "mid") if n["id"] == 103)
    assert ahri["chips"][0]["pp"] == pytest.approx(3.0)
    assert nodes(v, "jungle")[0]["chips"] == []
    assert set(trio_ids(v)) == {(64, 103, 89)}  # only trios through the locked node

    two = view(locks={"jungle": 64, "mid": 103})
    leona = next(n for n in nodes(two, "support") if n["id"] == 89)
    assert [(c["label"], round(c["pp"], 3)) for c in leona["chips"]] == [("J", 2.0), ("M", 1.0)]
    team = two["locked_team"]
    assert team["total_pp"] == pytest.approx(3.0) and len(team["pairs"]) == 1


def test_lock_off_pool_champion_adds_it():
    v = view(locks={"jungle": 122})
    assert [n["id"] for n in nodes(v, "jungle")] == [122]


def test_damage_profile_per_node():
    v = view()
    lee = nodes(v, "jungle")[0]
    assert lee["damage"] == {"phys": 0.8, "magic": 0.1, "true": 0.1}
    assert nodes(v, "jungle")[1]["damage"] is None  # Jarvan: no data


# ---------- 3. use current draft ----------
def live_draft(actions, our_slots):
    st = DraftState(get_format("tournament"), "blue", tuple(actions))
    return LiveDraft(st, tuple(our_slots), (), "BAN_PICK", 20.0, None, None, 1)


def test_current_draft_greys_out_and_excludes_from_trios():
    # tournament: B R B R B R bans, then B1 R1 R2 B2 B3 ...; blue bans Ahri, red picks Leona
    actions = [103, 0, 0, 0, 0, 0, 64, 89]
    live = live_draft(actions, [Slot(1, "jungle", 64, 0)])
    unavailable, locks = draft_context(live, {})
    assert locks == {"jungle": 64}  # our locked pick
    assert unavailable == {103, 89}  # Ahri banned, Leona picked by them; our own pick is a lock, not grey
    v = view(unavailable=unavailable, locks=locks)
    ahri = next(n for n in nodes(v, "mid") if n["id"] == 103)
    assert ahri["unavailable"] and ahri["chips"] == []
    assert all(103 not in ids and 89 not in ids for ids in trio_ids(v))
    assert [n["id"] for n in nodes(v, "jungle")] == [64]


def test_draft_lock_role_falls_back_to_engine_assignment():
    live = live_draft([0, 0, 0, 0, 0, 0, 64], [Slot(1, None, 64, 0)])
    _, locks = draft_context(live, {64: "jungle"})
    assert locks == {"jungle": 64}


# ---------- API ----------
@pytest.fixture
def client(make_services):
    svc = make_services()
    svc.synergy_override = source()
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        yield c, svc


def test_api_returns_graph_and_respects_threshold(client):
    c, _ = client
    body = c.post("/api/synergy", json={"roles": ROLES3, "threshold_pp": 1.0,
                                        "columns": {"jungle": [64, 59], "mid": [103, 61], "support": [89, 111]}}).json()
    assert [col["role"] for col in body["columns"]] == ROLES3
    assert len(body["edges"]) == 12
    assert {tuple(x["id"] for x in t["champs"]) for t in body["trios"]} == {(64, 103, 89), (59, 61, 111)}
    assert body["window"] == "16.21" and body["low_data_games"] == 30
    assert body["champions"] and {"id", "name", "icon"} <= set(body["champions"][0])
    high = c.post("/api/synergy", json={"roles": ROLES3, "threshold_pp": 1.5,
                                        "columns": {"jungle": [64, 59], "mid": [103, 61], "support": [89, 111]}}).json()
    assert len(high["trios"]) == 1


def test_api_lock_and_validation(client):
    c, _ = client
    body = c.post("/api/synergy", json={"roles": ROLES3, "locks": {"jungle": 64},
                                        "columns": {"jungle": [64, 59], "mid": [103], "support": [89]}}).json()
    assert [n["id"] for n in body["columns"][0]["nodes"]] == [64]
    assert c.post("/api/synergy", json={"roles": ["jungle"]}).status_code == 422  # needs 2+
    assert c.post("/api/synergy", json={"roles": ["jungle", "feeder"]}).status_code == 422


def test_api_use_current_draft(client):
    c, svc = client
    svc.live.current = live_draft([103, 0, 0, 0, 0, 0, 64, 89], [Slot(1, "jungle", 64, 0)])
    body = c.post("/api/synergy", json={"roles": ROLES3, "use_draft": True,
                                        "columns": {"jungle": [64, 59], "mid": [103, 61], "support": [89, 111]}}).json()
    assert body["draft"]["live"] is True
    mid = {n["id"]: n for n in body["columns"][1]["nodes"]}
    assert mid[103]["unavailable"] and not mid[61]["unavailable"]
    assert [n["id"] for n in body["columns"][0]["nodes"]] == [64]
    svc.live.current = None
    body = c.post("/api/synergy", json={"roles": ROLES3, "use_draft": True}).json()
    assert body["draft"]["live"] is False
