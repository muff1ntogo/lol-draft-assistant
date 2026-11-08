"""docs/strategy.md: runes and items from crawled matches, and Live's Strategy view."""
import json
from pathlib import Path

import pytest
from sqlmodel import Session

from draftassist.data.builds import (
    MIN_OPP_GAMES,
    load_static,
    parse_build,
    summarize,
)
from draftassist.data.crawler import parse_match
from draftassist.db.models import CrawledMatch, MatchParticipant
from draftassist.db.session import init_db
from draftassist.data.builds import store_build
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.strategy import BuildView, strategy_view
from draftassist.lcu.demo import demo_sessions

CHAMP, OPP, OTHER_OPP = 103, 61, 268  # Ahri vs Orianna / Azir, mid
ELECTROCUTE, FLEET = 8112, 8021
LUDENS, ZHONYAS, SORCS, MERC, MORELLO = 6655, 3157, 3020, 3111, 3165


def participant(champ, keystone=ELECTROCUTE, items=(LUDENS, ZHONYAS, SORCS, 0, 0, 0), spells=(4, 14)):
    return {"championId": champ, "summoner1Id": spells[0], "summoner2Id": spells[1],
            **{f"item{i}": it for i, it in enumerate(items)}, "item6": 3340,
            "perks": {"statPerks": {"offense": 5008, "flex": 5008, "defense": 5011},
                      "styles": [{"description": "primaryStyle", "style": 8100,
                                  "selections": [{"perk": keystone}, {"perk": 8139}, {"perk": 8138}, {"perk": 8135}]},
                                 {"description": "subStyle", "style": 8200,
                                  "selections": [{"perk": 8226}, {"perk": 8210}]}]}}


def test_parse_build():
    b = parse_build(participant(CHAMP))
    assert b["keystone"] == ELECTROCUTE and b["primary_style"] == 8100 and b["sub_style"] == 8200
    assert b["perks"] == [ELECTROCUTE, 8139, 8138, 8135, 8226, 8210]
    assert b["items"] == [LUDENS, ZHONYAS, SORCS] and b["spells"] == [4, 14]  # no trinket, no empty slots


def test_the_crawl_stores_builds(tmp_path):
    from test_data_pipeline import make_match

    m = make_match("EUW1_1", [1, 2, 3, 4, 5], [11, 12, 13, 14, 15])
    for p in m["info"]["participants"]:
        p.update({k: v for k, v in participant(p["championId"]).items() if k != "championId"})
    parsed = parse_match(m)
    assert len(parsed.builds) == 10 and parsed.builds[0]["keystone"] == ELECTROCUTE


def seed(db, n, keystone, win_every, opp=OPP, bracket="gold", patch="16.21", start=0, items=(LUDENS, ZHONYAS, SORCS)):
    with Session(db) as s:
        for i in range(start, start + n):
            mid = f"M{patch}-{bracket}-{i}"
            s.add(CrawledMatch(match_id=mid, patch=patch, bracket=bracket, queue_id=420, game_start_ms=0))
            s.flush()
            win = i % win_every == 0
            s.add(MatchParticipant(match_id=mid, champ_id=CHAMP, team_id=100, role="mid", win=win))
            s.add(MatchParticipant(match_id=mid, champ_id=opp, team_id=200, role="mid", win=not win))
            store_build(s.connection(), mid, CHAMP, parse_build(participant(CHAMP, keystone, (*items, 0, 0, 0)[:6])))
        s.commit()


@pytest.fixture
def main_db(tmp_path):
    return init_db(tmp_path / "main.sqlite")


def test_summary_from_the_bracket_alone(main_db):
    seed(main_db, 60, ELECTROCUTE, 2)
    seed(main_db, 20, FLEET, 4, start=60)
    s = summarize(main_db, None, "16.21", CHAMP, "mid", "gold")
    assert s.ref_label == "your bracket" and s.ref_games == 80
    assert [c.key for c in s.keystones] == [ELECTROCUTE, FLEET]
    assert s.keystones[0].share == pytest.approx(0.75) and s.keystones[0].wr == pytest.approx(0.5)
    assert {c.key for c in s.items} >= {LUDENS, ZHONYAS, SORCS}
    assert s.pages and json.loads(s.pages[0].key)[0] in (ELECTROCUTE, FLEET)


def test_master_says_what_the_bracket_says_how_well(main_db, tmp_path):
    master = init_db(tmp_path / "master.sqlite")
    seed(master, 50, FLEET, 2, bracket="master_plus")
    seed(main_db, 60, ELECTROCUTE, 2)
    seed(main_db, 50, FLEET, 5, start=60)
    s = summarize(main_db, master, "16.21", CHAMP, "mid", "gold")
    assert s.ref_label == "Master+" and s.keystones[0].key == FLEET and s.keystones[0].share == 1.0
    assert s.keystones[0].games == 50 and s.keystones[0].wr == pytest.approx(0.2)


def test_vs_opponent_only_with_enough_games(main_db):
    seed(main_db, MIN_OPP_GAMES + 10, ELECTROCUTE, 2, opp=OPP)
    seed(main_db, 5, FLEET, 2, opp=OTHER_OPP, start=500)
    assert summarize(main_db, None, "16.21", CHAMP, "mid", "gold", OPP).opponent == OPP
    s = summarize(main_db, None, "16.21", CHAMP, "mid", "gold", OTHER_OPP)
    assert s.opponent is None and s.ref_games == MIN_OPP_GAMES + 15


def test_a_small_bracket_falls_back_to_every_tier(main_db):
    seed(main_db, 10, ELECTROCUTE, 2, bracket="iron")
    seed(main_db, 60, ELECTROCUTE, 3, bracket="emerald", start=10)
    s = summarize(main_db, None, "16.21", CHAMP, "mid", "iron")
    assert s.bracket_games == 70


def test_older_patches_count_less(main_db):
    seed(main_db, 40, FLEET, 2, patch="16.20")
    seed(main_db, 40, ELECTROCUTE, 2, patch="16.21", start=100)
    s = summarize(main_db, None, "16.21", CHAMP, "mid", "gold")
    assert s.keystones[0].key == ELECTROCUTE and s.keystones[0].share == pytest.approx(40 / 60)


def test_no_games_no_summary(main_db):
    assert summarize(main_db, None, "16.21", CHAMP, "mid", "gold") is None


def write_static(cache: Path, version="16.21.1"):
    base = cache / "ddragon" / version / "en_US"
    base.mkdir(parents=True)
    items = {"data": {
        str(LUDENS): {"name": "Luden's Companion", "gold": {"total": 2900}, "tags": ["SpellDamage"], "depth": 3},
        str(ZHONYAS): {"name": "Zhonya's Hourglass", "gold": {"total": 3250}, "tags": ["Armor", "SpellDamage"], "depth": 3},
        str(SORCS): {"name": "Sorcerer's Shoes", "gold": {"total": 1100}, "tags": ["Boots"], "depth": 2},
        str(MERC): {"name": "Mercury's Treads", "gold": {"total": 1250}, "tags": ["Boots", "SpellBlock"], "depth": 2},
        str(MORELLO): {"name": "Morellonomicon", "gold": {"total": 2950}, "tags": ["SpellDamage"], "depth": 3,
                       "description": "Applies <status>Grievous Wounds</status>"},
        "1052": {"name": "Amplifying Tome", "gold": {"total": 400}, "tags": ["SpellDamage"], "into": [str(LUDENS)]}}}
    (base / "item.json").write_text(json.dumps(items))
    (base / "runesReforged.json").write_text(json.dumps([{"id": 8100, "name": "Domination", "slots": [
        {"runes": [{"id": ELECTROCUTE, "name": "Electrocute"}]}]}, {"id": 8000, "name": "Precision", "slots": [
        {"runes": [{"id": FLEET, "name": "Fleet Footwork"}]}]}]))
    (base / "summoner.json").write_text(json.dumps({"data": {"Flash": {"key": "4", "name": "Flash"},
                                                             "Dot": {"key": "14", "name": "Ignite"}}}))


def test_static_names_and_item_kinds(tmp_path):
    assert load_static(tmp_path).items == {}
    write_static(tmp_path)
    st = load_static(tmp_path)
    assert st.version == "16.21.1" and st.rune_name(ELECTROCUTE) == "Electrocute" and st.spell_name(14) == "Ignite"
    assert st.items[LUDENS].finished and not st.items[1052].finished
    assert st.items[SORCS].boots and not st.items[SORCS].finished
    assert st.items[MORELLO].anti_heal and not st.items[LUDENS].anti_heal


# ---------- Live ----------
def test_strategy_appears_per_lock_and_the_team_plan_at_the_end(make_services, main_db, tmp_path):
    svc = make_services()
    eng = svc.live.engine
    write_static(tmp_path)
    static = load_static(tmp_path)
    seed(main_db, 60, ELECTROCUTE, 2, items=(LUDENS, ZHONYAS, MORELLO, SORCS))
    calls = []

    def builds(champ, role, opp):
        calls.append((champ, role, opp))
        s = summarize(main_db, None, "16.21", CHAMP, "mid", "gold")
        return BuildView(s, static)

    svc.live.strategy_builds = builds
    views = [svc.live.view(e) for e in demo_sessions(eng, TOURNAMENT, "blue", game_id=9)]
    assert all(v["strategy"] is None for v in views if not v["our_slots"] or not any(s["champ"] for s in v["our_slots"]))
    first = next(v for v in views if v["strategy"])
    assert len(first["strategy"]["players"]) == 1 and not first["strategy"]["complete"]
    last = views[-1]["strategy"]
    assert last["complete"] and len(last["players"]) == 5
    assert last["team"]["lines"] and len(last["threats"]) == 2
    p = last["players"][0]
    assert p["lane"]["summary"] and p["opponent"] is not None
    b = p["build"]
    assert b["keystones"][0]["name"] == "Electrocute" and b["core"][0]["name"] in {"Luden's Companion", "Zhonya's Hourglass", "Morellonomicon"}
    assert b["boots"]["name"] == "Sorcerer's Shoes" and b["page"]["runes"][0] == "Electrocute"
    assert b["spells"]["names"] == ["Flash", "Ignite"]
    assert any(opp is not None for _, _, opp in calls)


def test_a_failing_build_reader_hides_only_the_strategy(make_services, monkeypatch):
    monkeypatch.delenv("DRAFTASSIST_STRICT")
    svc = make_services()
    svc.live.strategy_builds = lambda *a: 1 / 0
    v = svc.live.view(demo_sessions(svc.live.engine, TOURNAMENT, "blue", game_id=9)[-1])
    assert v["strategy"] is None and "strategy" in v["panel_errors"] and v["center"]["mode"] == "locked"


class FakeStats:
    def __init__(self, gold, edge=0.0):
        self.gold, self.edge = gold, edge

    def lane_gold_of(self, role, a, b):
        return self.gold

    def matchup_edge(self, role, a, b):
        return self.edge


class FakeKit:
    def __init__(self, values):
        self.values = values  # (champ, tag) -> intensity

    def intensity(self, champ, role, tag):
        return self.values.get((champ, tag), 0.0)


def fake_engine(gold, kit=None, ranges=None, labels=None):
    from types import SimpleNamespace

    from draftassist.engine.comp import EMPTY_PROFILES

    traits = SimpleNamespace(kit=FakeKit(kit or {}), attack_range=ranges or {})
    data = SimpleNamespace(name=lambda c: {1: "Us", 2: "Them"}.get(c, str(c)), traits=traits, labels=labels or {},
                           profiles=EMPTY_PROFILES, damage={})
    return SimpleNamespace(data=data, our_stats=lambda role: FakeStats(gold))


def test_lane_plan_reads_gold_and_kit():
    from draftassist.engine.strategy import lane_plan

    plan = lane_plan(fake_engine(700), "mid", 1, 2)
    assert "favoured early" in plan["summary"] and "+700" in plan["summary"] and plan["gold15"] == 700
    assert "Them is favoured early" in lane_plan(fake_engine(-300), "mid", 1, 2)["summary"]
    assert "Even lane" in lane_plan(fake_engine(100), "mid", 1, 2)["summary"]
    assert lane_plan(fake_engine(0), "mid", 1, None)["summary"] == "Their laner isn't locked yet."
    eng = fake_engine(0, kit={(2, "hard_gap_close"): 1.0, (2, "sustain"): 0.7}, ranges={1: 125, 2: 550})
    lines = " ".join(lane_plan(eng, "top", 1, 2)["lines"])
    assert "out-range you" in lines and "hard engage" in lines and "out-sustains" in lines


def test_build_reasons_follow_their_damage_and_healing(main_db, tmp_path):
    from draftassist.engine.strategy import build_plan

    write_static(tmp_path)
    seed(main_db, 60, ELECTROCUTE, 2, items=(LUDENS, ZHONYAS, SORCS, MORELLO))
    seed(main_db, 30, ELECTROCUTE, 2, items=(LUDENS, ZHONYAS, MERC), start=100)
    view = BuildView(summarize(main_db, None, "16.21", CHAMP, "mid", "gold"), load_static(tmp_path))
    eng = fake_engine(0, kit={(11, "sustain"): 1.0, (12, "sustain"): 0.8})
    eng.data.damage = {(11, "top"): (0.3, 0.7, 0.0), (12, "jungle"): (0.3, 0.7, 0.0)}
    plan = build_plan(eng, "mid", CHAMP, view, {11: "top", 12: "jungle"}, None)
    assert [i["name"] for i in plan["core"]][:2] == ["Luden's Companion", "Zhonya's Hourglass"]
    assert any("anti-heal" in r for r in plan["reasons"])
    assert plan["situational"] == [] or all(i["why"] for i in plan["situational"])


# ---------- decision 8 Nov 2026: "damage-heavy" from the crawl ----------
def test_damage_thresholds_from_crawled_teams(main_db):
    from draftassist.data.builds import MIN_TEAMS, damage_thresholds

    assert damage_thresholds(main_db) is None  # nothing crawled
    with Session(main_db) as s:
        for i in range(MIN_TEAMS):
            mid = f"D{i}"
            s.add(CrawledMatch(match_id=mid, patch="16.21", bracket="gold", queue_id=420, game_start_ms=i))
            s.flush()
            magic = i / MIN_TEAMS  # teams spread evenly from all-physical to all-magic
            for team in (100, 200):
                for k in range(5):
                    s.add(MatchParticipant(match_id=mid, champ_id=team + k, team_id=team, role="top", win=team == 100,
                                           dmg_phys=round(10_000 * (1 - magic)), dmg_magic=round(10_000 * magic), dmg_true=0))
        s.commit()
    t = damage_thresholds(main_db)
    assert t.teams == 2 * MIN_TEAMS
    assert t.ap_heavy == pytest.approx(0.8, abs=0.01) and t.ad_heavy == pytest.approx(0.8, abs=0.01)
    assert t.median_ap == pytest.approx(0.5, abs=0.01)


def test_build_reasons_use_the_crawled_thresholds(main_db, tmp_path):
    from types import SimpleNamespace

    from draftassist.engine.strategy import build_plan

    write_static(tmp_path)
    seed(main_db, 60, ELECTROCUTE, 2, items=(LUDENS, ZHONYAS, SORCS, MERC))
    summary = summarize(main_db, None, "16.21", CHAMP, "mid", "gold")
    eng = fake_engine(0)
    eng.data.damage = {(11, "top"): (0.45, 0.55, 0.0)}
    loose = BuildView(summary, load_static(tmp_path), SimpleNamespace(ap_heavy=0.5, ad_heavy=0.7))
    strict = BuildView(summary, load_static(tmp_path), SimpleNamespace(ap_heavy=0.6, ad_heavy=0.7))
    assert any("magic" in r for r in build_plan(eng, "mid", CHAMP, loose, {11: "top"}, None)["reasons"])
    assert not any("magic" in r for r in build_plan(eng, "mid", CHAMP, strict, {11: "top"}, None)["reasons"])
