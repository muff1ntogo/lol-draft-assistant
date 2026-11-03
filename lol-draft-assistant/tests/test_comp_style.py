"""Comp type and fight style: game length and power curves (docs/comp-style.md, Stage A), and fight style as
the five comp identities derived from the reviewed tags (the merge in docs/synergy-v2.md)."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from draftassist.api.main import create_app
from draftassist.data.aggregate import aggregate
from draftassist.data.backfill import backfill_durations
from draftassist.data.crawler import Crawler, parse_match
from draftassist.data.profiles import load_profiles, profiles_from_traits, style_from_classes
from draftassist.data.tags import sync_tags
from draftassist.data.riot import cache_path
from draftassist.db.models import Champion, ChampRoleStats, CrawledMatch
from draftassist.engine.comp import LONG_S, SHORT_S, power_curve, timing_label

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
BLUE = (1, 2, 3, 4, 5)
RED = (11, 12, 13, 14, 15)


def match(match_id, blue_win=True, duration=1800, blue=BLUE, red=RED):
    participants = []
    for team, champs, win in ((100, blue, blue_win), (200, red, not blue_win)):
        for champ, pos in zip(champs, POSITIONS):
            participants.append({"championId": champ, "teamId": team, "teamPosition": pos, "win": win})
    return {"metadata": {"matchId": match_id},
            "info": {"gameVersion": "16.21.712.4501", "queueId": 420, "gameDuration": duration,
                     "gameStartTimestamp": 1_793_000_000_000, "participants": participants,
                     "teams": [{"teamId": 100, "bans": []}, {"teamId": 200, "bans": []}]}}


def store(db, raw, bracket="gold"):
    c = Crawler.__new__(Crawler)
    c.db = db
    c._store(parse_match(raw), bracket)


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        for c, key, tags in ((1, "Malphite", ["Tank", "Fighter"]), (2, "Vi", ["Fighter", "Assassin"]),
                             (3, "Xerath", ["Mage"]), (4, "Jinx", ["Marksman"]), (5, "Lulu", ["Support", "Mage"]),
                             (11, "Newchamp", ["Assassin"]), (12, "C12", []), (13, "C13", []), (14, "C14", []),
                             (15, "C15", [])):
            s.add(Champion(champ_id=c, key=key, name=key, patch="16.21.1", tags=tags))
        s.commit()
    return engine


# ---------- power curve ----------
def test_power_curve_sign_and_shrinkage():
    p = 0.5
    early = power_curve(wins_short=360, games_short=600, wins_long=240, games_long=600, p=p)  # 60% short, 40% long
    assert early < -0.5 and timing_label(early) == "early"
    late = power_curve(wins_short=240, games_short=600, wins_long=360, games_long=600, p=p)
    assert late == pytest.approx(-early) and timing_label(late) == "late"
    strong = power_curve(330, 600, 330, 600, p=0.55)  # simply strong: wins 55% at every length
    assert strong == pytest.approx(0.0, abs=1e-9) and timing_label(strong) == "mid"
    thin = power_curve(3, 3, 0, 3, p=0.5)  # 100% vs 0% on 3 games each: shrunk towards nothing
    assert abs(thin) < 0.25
    assert timing_label(None) is None


def test_game_length_is_stored_and_bucketed(db):
    # blue top (champ 1) wins every short game and loses every long one
    for i in range(10):
        store(db, match(f"EUW1_S{i}", blue_win=True, duration=SHORT_S - 60))
        store(db, match(f"EUW1_L{i}", blue_win=False, duration=LONG_S + 60))
    store(db, match("EUW1_M", blue_win=True, duration=(SHORT_S + LONG_S) // 2))  # neither bucket
    aggregate(db, "16.21")
    with Session(db) as s:
        assert s.get(CrawledMatch, "EUW1_S0").game_duration_s == SHORT_S - 60
        row = s.get(ChampRoleStats, ("16.21", "gold", 1, "top"))
        assert (row.games_short, row.wins_short, row.games_long, row.wins_long) == (10, 10, 10, 0)
        assert row.games == 21
    prof = load_profiles(db, "16.21")
    assert prof.curve(1, "top").curve < 0 and prof.curve(1, "top").games == 20
    assert prof.curve(11, "top").curve > 0  # red top won the long games


def test_timing_needs_enough_games(db):
    store(db, match("EUW1_S", duration=SHORT_S - 60))
    store(db, match("EUW1_L", blue_win=False, duration=LONG_S + 60))
    aggregate(db, "16.21")
    tc = load_profiles(db, "16.21").curve(1, "top")
    assert tc.games == 2 and tc.label is None  # too few games for a label
    assert load_profiles(db, "16.21").curve(99, "top") is None


# ---------- fight style from tags (the merge) ----------
from draftassist.engine.comp import STYLES, badge, team_comp  # noqa: E402
from draftassist.engine.traits import TAGS, ChampionTraits, TagEntry  # noqa: E402


def tags(reviewed=False, **v):
    return TagEntry({**dict.fromkeys(TAGS, 0), **v}, reviewed)


def shares(st):
    return dict(zip(STYLES, st.weights))


def test_identity_shares_come_from_the_tags_as_written():
    traits = ChampionTraits({(89, "support"): {"peel_all": 0.0, "frontline_all": 0.9}},
                            {(89, "support"): tags(engage=3, pick=3),  # unreviewed: read as written for labels
                             (117, "support"): tags(True, enchanter=3, disengage=2)}, {})
    prof = profiles_from_traits(traits)
    leona = shares(prof.style(89))
    assert leona["engage"] == pytest.approx(0.5) and leona["pick"] == pytest.approx(0.5)
    assert prof.style(89).frontline and prof.style(89).source == "tags" and not prof.style(89).reviewed
    lulu = shares(prof.style(117))
    assert lulu["protect"] == pytest.approx(1.0) and prof.style(117).reviewed
    assert sum(prof.style(117).weights) == pytest.approx(1.0)
    assert badge(prof, 89, "support").styles == ("engage", "pick")
    assert badge(prof, 117, "support").text() == "Protect"


def test_style_depends_on_the_role_tagged():
    traits = ChampionTraits({}, {(80, "top"): tags(splitpush=3), (80, "support"): tags(engage=3)}, {})
    prof = profiles_from_traits(traits)
    assert prof.style(80, "top").weights[STYLES.index("split")] == pytest.approx(1.0)
    assert prof.style(80, "support").weights[STYLES.index("engage")] == pytest.approx(1.0)


def test_untagged_champions_fall_back_to_classes():
    weights, frontline = style_from_classes(["Assassin"])
    assert dict(zip(STYLES, weights)) == {"engage": 0.3, "poke": 0.0, "pick": 0.7, "protect": 0.0, "split": 0.0}
    assert not frontline
    weights, frontline = style_from_classes(["Tank", "Fighter"])
    assert sum(weights) == pytest.approx(1.0) and frontline
    prof = profiles_from_traits(ChampionTraits({}, {}, {}), class_tags={11: ["Assassin"]})
    assert prof.style(11).source == "classes" and prof.style(11).unsure


def test_team_comp_names_an_identity_with_two_contributors():
    traits = ChampionTraits({}, {(1, "top"): tags(engage=3), (2, "jungle"): tags(engage=3, dive=3),
                                 (3, "mid"): tags(poke=3), (4, "adc"): tags(), (5, "support"): tags(engage=2)}, {})
    prof = profiles_from_traits(traits)
    comp = team_comp(prof, [(1, "top"), (2, "jungle"), (3, "mid"), (5, "support")])
    assert comp.style == "engage" and comp.shares["engage"] > 0.6
    assert team_comp(prof, [(1, "top"), (3, "mid")]).style == "mixed"  # one engage champion is not a plan


def test_load_profiles_reads_the_tags_table(db, tmp_path):
    path = tmp_path / "champion_tags.yaml"
    path.write_text("Malphite:\n  top: {engage: 3, reviewed: true}\nXerath:\n  mid: {poke: 3, pick: 1}\n",
                    encoding="utf-8")
    sync_tags(db, path)
    prof = load_profiles(db, "16.21")
    assert prof.style(1).source == "tags" and prof.style(1).reviewed
    assert shares(prof.style(1))["engage"] == pytest.approx(1.0)
    assert shares(prof.style(3))["poke"] == pytest.approx(0.75)
    assert prof.style(11).source == "classes"  # Newchamp: no tags
    assert prof.style(1).frontline  # Malphite's Tank class


# ---------- backfill game length from the Riot cache ----------
def test_backfill_durations_from_cache_is_idempotent(db, tmp_path):
    raw = match("EUW1_1", duration=1500)
    store(db, raw)
    with Session(db) as s:
        row = s.get(CrawledMatch, "EUW1_1")
        row.game_duration_s = None  # stored before game length was recorded
        s.add(row)
        s.commit()
    store(db, match("EUW1_2"))
    with Session(db) as s:
        row = s.get(CrawledMatch, "EUW1_2")
        row.game_duration_s = None
        s.add(row)
        s.commit()
    path = cache_path(tmp_path / "riot", "europe", "/lol/match/v5/matches/EUW1_1", None)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"fetched_at": 0, "body": raw}), encoding="utf-8")
    assert backfill_durations(db, tmp_path) == {"updated": 1, "missing": 1}
    assert backfill_durations(db, tmp_path) == {"updated": 0, "missing": 1}
    with Session(db) as s:
        assert s.get(CrawledMatch, "EUW1_1").game_duration_s == 1500


# ---------- API: the Tags page shows timing and fight style ----------
def test_tags_api_shows_timing_and_identity(make_services, db, tmp_path):
    path = tmp_path / "champion_tags.yaml"
    path.write_text("Malphite:\n  top: {engage: 3, reviewed: true}\n", encoding="utf-8")
    sync_tags(db, path)
    with Session(db) as s:
        s.get(Champion, 1).roles = ["top"]
        s.commit()
    svc = make_services(db=db)
    svc.tags_path = path
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        body = c.get("/api/tags").json()
        assert body["styles"]["engage"] == "Engage"
        malphite = next(r for r in body["champions"] if r["champ_id"] == 1)
        assert malphite["identity"]["top"]["badge"]["styles"] == ["engage"]
        assert malphite["identity"]["top"]["source"] == "tags"
        assert "top" in malphite["timing"]
        assert c.get("/api/champions/styles").status_code in (404, 405)  # the old review is gone
