"""Synergy v2, Stage 1: champion traits (measured from match stats + reviewed judgement tags).
See docs/synergy-v2.md."""
import json
import random

import pytest
from sqlmodel import Session, select

from draftassist.data.aggregate import aggregate
from draftassist.data.backfill import backfill_traits
from draftassist.data.crawler import parse_match
from draftassist.data.ddragon import parse_champions
from draftassist.data.riot import cache_path
from draftassist.data.tags import dump_tags_yaml, load_tags, load_tags_yaml, sync_tags, update_tag
from draftassist.data.traits import load_traits
from draftassist.data.traits_map import MEASURED_TRAITS, PARTICIPANT_TRAIT_COLUMNS, parse_traits
from draftassist.db.models import Champion, ChampionTag, ChampionTraitRow, MatchParticipant
from draftassist.engine.traits import (
    TAGS,
    ChampionTraits,
    TagEntry,
    percentiles,
    power_curve_slope,
    ranged,
    ranged_long,
)

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
BLUE = (1, 2, 3, 4, 5)
RED = (11, 12, 13, 14, 15)


def participant(champ, team, pos, win, *, cc=10, immob=5, taken=20_000, mitigated=15_000, heals=0, shields=0,
                lane=1, challenges=True, taken_pct=None):
    p = {"championId": champ, "teamId": team, "teamPosition": pos, "win": win,
         "physicalDamageDealtToChampions": 10_000, "magicDamageDealtToChampions": 5_000,
         "trueDamageDealtToChampions": 1_000,
         "timeCCingOthers": cc, "totalDamageTaken": taken, "damageSelfMitigated": mitigated,
         "totalHealsOnTeammates": heals, "totalDamageShieldedOnTeammates": shields}
    if challenges:
        p["challenges"] = {"enemyChampionImmobilizations": immob, "laningPhaseGoldExpAdvantage": lane}
        if taken_pct is not None:
            p["challenges"]["damageTakenOnTeamPercentage"] = taken_pct
    return p


def match(match_id, blue=BLUE, red=RED, blue_win=True, duration=1800, version="16.21.712.4501", per_role=None,
          challenges=True):
    """`per_role`: role index -> participant kwargs, applied to both teams."""
    per_role = per_role or {}
    parts = []
    for team, champs, win in ((100, blue, blue_win), (200, red, not blue_win)):
        for i, (champ, pos) in enumerate(zip(champs, POSITIONS)):
            parts.append(participant(champ, team, pos, win, challenges=challenges, **per_role.get(i, {})))
    return {"metadata": {"matchId": match_id},
            "info": {"gameVersion": version, "queueId": 420, "gameDuration": duration,
                     "gameStartTimestamp": 1_793_000_000_000, "participants": parts,
                     "teams": [{"teamId": 100, "bans": []}, {"teamId": 200, "bans": []}]}}


def store(db, raw, bracket="gold"):
    from draftassist.data.crawler import Crawler

    c = Crawler.__new__(Crawler)
    c.db = db
    c._store(parse_match(raw), bracket)


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        for c in BLUE + RED:
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1", attack_range=550 if c % 2 else 125))
        s.commit()
    return engine


# ---------- 1a: the field mapping ----------
def test_trait_mapping_reads_a_fixture_match():
    raw = match("EUW1_1", per_role={0: dict(cc=30, immob=12, taken=40_000, mitigated=50_000, lane=1),
                                    4: dict(heals=3_000, shields=2_000, taken=5_000)})
    m = parse_match(raw)
    assert len(m.traits) == 10
    top, sup = m.traits[0], m.traits[4]
    assert set(top) == set(PARTICIPANT_TRAIT_COLUMNS)
    assert top["cc_time"] == 30 and top["immobilizations"] == 12
    assert top["self_mitigated"] == 50_000
    assert top["lane_adv"] == 1
    assert sup["heal_shield"] == 5_000
    # share of the team's damage taken: 40k of 40k + 3*20k + 5k
    assert top["dmg_taken_share"] == pytest.approx(40_000 / 105_000)


def test_trait_mapping_prefers_the_challenges_share_when_present():
    raw = match("EUW1_1", per_role={0: dict(taken_pct=0.31)})
    assert parse_match(raw).traits[0]["dmg_taken_share"] == pytest.approx(0.31)


def test_missing_challenges_gives_null_not_an_error():
    traits = parse_match(match("EUW1_1", challenges=False)).traits
    assert traits[0]["immobilizations"] is None
    assert traits[0]["lane_adv"] is None
    assert traits[0]["cc_time"] == 10  # top-level fields still read
    p = participant(1, 100, "TOP", True)
    del p["totalHealsOnTeammates"], p["totalDamageShieldedOnTeammates"]
    assert parse_traits(p, [p])["heal_shield"] is None


def test_lane_pressure_falls_back_to_the_early_laning_key():
    p = participant(1, 100, "TOP", True)
    del p["challenges"]["laningPhaseGoldExpAdvantage"]
    p["challenges"]["earlyLaningPhaseGoldExpAdvantage"] = 1
    assert parse_traits(p, [p])["lane_adv"] == 1


def test_crawler_stores_trait_columns(db):
    store(db, match("EUW1_1", per_role={0: dict(cc=30)}))
    with Session(db) as s:
        row = s.get(MatchParticipant, ("EUW1_1", 1))
        assert row.cc_time == 30 and row.immobilizations == 5 and row.lane_adv == 1


def test_attack_range_from_data_dragon():
    payload = {"data": {"Jinx": {"id": "Jinx", "key": "222", "name": "Jinx", "tags": ["Marksman"],
                                 "stats": {"attackrange": 525}}}}
    (champ,) = parse_champions(payload, "16.21.1")
    assert champ.attack_range == 525
    assert ranged(525) == 1 and ranged(175) == 0 and ranged(None) is None
    assert ranged_long(650) == 1 and ranged_long(500) == 0 and ranged_long(575) == pytest.approx(0.5)


def test_backfill_traits_from_cache_is_idempotent(db, tmp_path):
    from sqlalchemy import text

    raw = match("EUW1_1", per_role={0: dict(cc=42)})
    store(db, raw)
    with db.begin() as conn:  # stored before trait sources were recorded
        conn.execute(text("UPDATE match_participants SET " + ", ".join(f"{c} = NULL" for c in PARTICIPANT_TRAIT_COLUMNS)))
    with Session(db) as s:
        assert s.get(MatchParticipant, ("EUW1_1", 1)).cc_time is None
    from draftassist.data.riot import REGION, match_path

    path = cache_path(tmp_path / "riot", REGION, match_path("EUW1_1"), None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"body": raw}), encoding="utf-8")
    assert backfill_traits(db, tmp_path) == {"updated": 1, "missing": 0}
    with Session(db) as s:
        assert s.get(MatchParticipant, ("EUW1_1", 1)).cc_time == 42
    assert backfill_traits(db, tmp_path) == {"updated": 0, "missing": 0}


# ---------- 1a: percentiles and the power curve ----------
def test_percentiles_are_in_unit_interval_and_handle_ties_and_nulls():
    p = percentiles({"a": 1.0, "b": 2.0, "c": 2.0, "d": 5.0, "e": None})
    assert p["e"] is None
    assert p["a"] == 0.0 and p["d"] == 1.0
    assert p["b"] == p["c"] == pytest.approx(0.5)
    assert percentiles({"x": 3.0}) == {"x": 0.5}
    rng = random.Random(1)
    vals = {i: rng.random() for i in range(50)}
    assert all(0 <= v <= 1 for v in percentiles(vals).values())


def test_traits_are_percentiles_computed_per_role(db):
    # Top 1 CCs more than top 11; supports out-CC everyone in raw terms but are ranked only among supports.
    for i in range(30):
        store(db, match(f"EUW1_{i}", blue_win=i % 2 == 0, per_role={4: dict(cc=200, heals=5_000)}))
        store(db, match(f"EUW1_x{i}", blue=(1, 2, 3, 4, 5), red=(11, 12, 13, 14, 15), blue_win=i % 2 == 1,
                        per_role={0: dict(cc=50)}))
    aggregate(db, "16.21")
    traits = load_traits(db)
    rows = list(traits.measured.values())
    assert rows
    for t in rows:
        for name in ("cc", "frontline", "peel", "lane_pressure", "power_curve"):
            v = t.get(name)
            assert v is None or 0.0 <= v <= 1.0
        assert t["dmg_phys"] + t["dmg_magic"] + t["dmg_true"] == pytest.approx(1.0)
    # every role has its own 0..1 spread: both supports are 0 or 1, not squeezed by other roles
    sups = sorted(traits.measured[(c, "support")]["cc"] for c in (5, 15))
    assert sups == [0.5, 0.5]  # identical stats: a tie within the role
    assert traits.measured[(1, "top")]["cc"] == traits.measured[(11, "top")]["cc"]
    assert traits.measured[(1, "top")]["ranged"] == 1 and traits.measured[(2, "jungle")]["ranged"] == 0
    with Session(db) as s:
        assert s.exec(select(ChampionTraitRow).where(ChampionTraitRow.bracket == "gold")).first() is not None


def test_cc_percentile_ranks_within_the_role(db):
    for i in range(30):
        # blue top CCs a lot, red top little; supports CC even more but are another role
        raw = match(f"EUW1_{i}", blue_win=i % 2 == 0, per_role={4: dict(cc=500)})
        for p in raw["info"]["participants"]:
            if p["championId"] == 1:
                p["timeCCingOthers"], p["challenges"]["enemyChampionImmobilizations"] = 60, 20
            if p["championId"] == 11:
                p["timeCCingOthers"], p["challenges"]["enemyChampionImmobilizations"] = 2, 1
        store(db, raw)
    aggregate(db, "16.21")
    m = load_traits(db).measured
    assert m[(1, "top")]["cc"] == 1.0 and m[(11, "top")]["cc"] == 0.0


def test_power_curve_is_negative_for_an_early_champion():
    # wins short games, loses long ones: (wins, games) in < 25, 25-32, > 32 min
    early = power_curve_slope([(140, 200), (100, 200), (60, 200)])
    late = power_curve_slope([(60, 200), (100, 200), (140, 200)])
    flat = power_curve_slope([(100, 200), (100, 200), (100, 200)])
    assert early < 0 < late
    assert flat == pytest.approx(0.0, abs=1e-9)
    assert power_curve_slope([(5, 10), (5, 10), (5, 10)]) is None  # too few games


def test_power_curve_from_crawled_game_lengths(db):
    for i in range(60):
        short = i < 30
        # champion 1 (blue top) wins every short game and loses every long one
        store(db, match(f"EUW1_{i}", blue_win=short, duration=20 * 60 if short else 38 * 60))
    aggregate(db, "16.21")
    m = load_traits(db).measured
    assert m[(1, "top")]["power_curve_slope"] < 0 < m[(11, "top")]["power_curve_slope"]
    assert m[(1, "top")]["power_curve"] < m[(11, "top")]["power_curve"]


def test_missing_source_field_gives_null_trait(db):
    for i in range(30):
        store(db, match(f"EUW1_{i}", blue_win=i % 2 == 0, challenges=False))
    aggregate(db, "16.21")
    t = load_traits(db).measured[(1, "top")]
    assert t["lane_pressure"] is None  # its only source is a challenges key
    assert t["cc"] is not None  # timeCCingOthers alone still gives cc


def test_measured_traits_list_is_complete():
    assert set(MEASURED_TRAITS) == {"cc", "frontline", "peel", "dmg_phys", "dmg_magic", "dmg_true",
                                    "power_curve", "lane_pressure", "ranged"}


# ---------- 1b: judgement tags ----------
YAML_TEXT = """\
# test tags
Leona:
  support: {engage: 3, pick: 2, reviewed: true}
Lulu:
  support: {enchanter: 3, disengage: 2}
Jinx:
  adc: {hypercarry: 3, reviewed: false}
"""


@pytest.fixture
def tag_db(engine):
    with Session(engine) as s:
        for cid, key in ((89, "Leona"), (117, "Lulu"), (222, "Jinx"), (999, "Newchamp")):
            s.add(Champion(champ_id=cid, key=key, name=key, patch="16.21.1"))
        s.commit()
    return engine


def test_tags_load_from_yaml(tmp_path, tag_db):
    path = tmp_path / "champion_tags.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    raw = load_tags_yaml(path)
    assert raw["Leona"]["support"].values["engage"] == 3
    assert raw["Leona"]["support"].reviewed is True
    assert raw["Lulu"]["support"].reviewed is False  # unmarked = unreviewed
    assert raw["Lulu"]["support"].values["poke"] == 0  # missing tags are 0
    result = sync_tags(tag_db, path)
    assert result["loaded"] == 3 and result["unknown"] == []
    tags = load_tags(tag_db)
    assert tags[(89, "support")].values["engage"] == 3
    with Session(tag_db) as s:
        assert len(s.exec(select(ChampionTag)).all()) == 3


def test_unreviewed_tags_count_at_half_weight():
    reviewed = TagEntry({**dict.fromkeys(TAGS, 0), "engage": 3}, reviewed=True)
    unreviewed = TagEntry({**dict.fromkeys(TAGS, 0), "engage": 3}, reviewed=False)
    t = ChampionTraits({}, {(1, "support"): reviewed, (2, "support"): unreviewed}, {})
    assert t.intensity(1, "support", "engage") == pytest.approx(1.0)
    assert t.intensity(2, "support", "engage") == pytest.approx(0.5)
    assert t.intensity(1, "support", "not engage") == pytest.approx(0.0)
    assert t.intensity(2, "support", "pick") == 0.0


def test_untagged_champions_fall_back_to_measured_traits():
    t = ChampionTraits({(7, "mid"): {"cc": 0.8, "power_curve": 0.9, "ranged": 1}}, {}, {7: 550})
    assert t.untagged(7)
    assert t.intensity(7, "mid", "engage") is None  # no tag: unknown, not 0
    assert t.intensity(7, "mid", "cc") == pytest.approx(0.8)
    assert t.intensity(7, "mid", "power_curve_late") == pytest.approx(0.9)
    assert t.intensity(7, "mid", "power_curve_early") == pytest.approx(0.1)
    assert t.intensity(7, "mid", "not ranged_long") == pytest.approx(1 - ranged_long(550))


def test_tags_fall_back_to_another_role_of_the_same_champion():
    e = TagEntry({**dict.fromkeys(TAGS, 0), "poke": 3}, reviewed=True)
    t = ChampionTraits({}, {(99, "mid"): e}, {})
    assert t.intensity(99, "support", "poke") == pytest.approx(1.0)
    assert not t.untagged(99)


def test_tag_edit_round_trips_to_yaml(tmp_path, tag_db):
    path = tmp_path / "champion_tags.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    sync_tags(tag_db, path)
    update_tag(tag_db, path, 117, "support", {"enchanter": 2, "disengage": 3}, reviewed=True)
    update_tag(tag_db, path, 999, "top", {"splitpush": 3}, reviewed=False)  # a new champion
    raw = load_tags_yaml(path)
    assert raw["Lulu"]["support"].values["enchanter"] == 2 and raw["Lulu"]["support"].reviewed
    assert raw["Newchamp"]["top"].values["splitpush"] == 3
    assert raw["Leona"]["support"].values["engage"] == 3  # untouched entries survive
    assert load_tags(tag_db)[(117, "support")].reviewed
    with pytest.raises(ValueError):
        update_tag(tag_db, path, 117, "support", {"enchanter": 5}, reviewed=True)  # 0-3 only
    with pytest.raises(ValueError):
        update_tag(tag_db, path, 117, "support", {"wombo": 1}, reviewed=True)
    # dumping what was loaded is stable
    text1 = path.read_text(encoding="utf-8")
    dump_tags_yaml(load_tags_yaml(path), path)
    assert path.read_text(encoding="utf-8") == text1


def test_shipped_tag_file_is_valid_and_uses_real_champion_keys():
    from draftassist.data.tags import TAGS_FILE

    raw = load_tags_yaml(TAGS_FILE)
    assert len(raw) >= 150
    roles = {"top", "jungle", "mid", "adc", "support"}
    for key, by_role in raw.items():
        assert key.isalnum(), key
        for role, entry in by_role.items():
            assert role in roles, (key, role)
            assert all(0 <= v <= 3 for v in entry.values.values())
            assert not entry.reviewed  # drafted by Claude: the user reviews them


# ---------- Tags page API ----------
def test_tags_api_lists_edits_and_flags_untagged(make_services, tag_db, tmp_path):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    path = tmp_path / "champion_tags.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    sync_tags(tag_db, path)
    svc = make_services(db=tag_db)
    svc.tags_path = path
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        body = c.get("/api/tags").json()
        assert body["tags"] == list(TAGS)
        rows = {r["champ_id"]: r for r in body["champions"]}
        assert rows[89]["entries"]["support"]["values"]["engage"] == 3
        assert rows[89]["entries"]["support"]["reviewed"] is True
        assert rows[999]["untagged"] and not rows[89]["untagged"]
        assert body["counts"] == {"champions": 4, "entries": 3, "reviewed": 1, "untagged": 1}
        put = c.put("/api/tags/117/support", json={"values": {"enchanter": 3, "disengage": 1}, "reviewed": True})
        assert put.status_code == 200 and put.json()["entries"]["support"]["reviewed"] is True
        assert load_tags_yaml(path)["Lulu"]["support"].reviewed
        assert c.put("/api/tags/117/support", json={"values": {"enchanter": 9}, "reviewed": True}).status_code == 422
        assert c.delete("/api/tags/117/support").json()["untagged"] is True
        assert "Lulu" not in load_tags_yaml(path)
