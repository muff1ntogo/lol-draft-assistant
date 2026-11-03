"""Comp type and team-fight style, Stage A: game length, power curves, the style seed, user overrides.
See docs/comp-style.md."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from draftassist.api.main import create_app
from draftassist.data.aggregate import aggregate
from draftassist.data.backfill import backfill_durations
from draftassist.data.crawler import Crawler, parse_match
from draftassist.data.profiles import (
    load_profiles,
    load_seed,
    reset_style,
    save_style,
    style_from_classes,
)
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


# ---------- style seed and overrides ----------
def test_seed_is_valid_and_covers_the_roster():
    seed = load_seed()
    assert len(seed.champions) >= 170
    for key, (weights, frontline) in seed.champions.items():
        assert sum(weights) == pytest.approx(1.0), key
        assert all(0 <= w <= 1 for w in weights) and isinstance(frontline, bool)
    assert seed.unsure <= set(seed.champions)
    assert seed.champions["Malphite"][1] is True and seed.champions["Xerath"][0] == (0.0, 0.0, 1.0)


def test_style_falls_back_to_classes_for_champions_missing_from_the_seed(db):
    weights, frontline = style_from_classes(["Assassin"])
    assert weights == (0.0, 1.0, 0.0) and not frontline
    weights, frontline = style_from_classes(["Tank", "Fighter"])
    assert sum(weights) == pytest.approx(1.0) and frontline
    new = load_profiles(db, "16.21").style(11)
    assert new.source == "classes" and new.weights == (0.0, 1.0, 0.0) and new.unsure


def test_user_override_beats_the_seed_and_reset_restores_it(db):
    seeded = load_profiles(db, "16.21").style(1)  # Malphite
    assert seeded.source == "seed" and not seeded.reviewed
    save_style(db, 1, (0.6, 0.4, 0.0), frontline=True)
    mine = load_profiles(db, "16.21").style(1)
    assert mine.source == "user" and mine.weights == (0.6, 0.4, 0.0) and mine.reviewed
    reset_style(db, 1)
    assert load_profiles(db, "16.21").style(1) == seeded
    with pytest.raises(ValueError):
        save_style(db, 1, (0.6, 0.6, 0.0), frontline=True)  # doesn't sum to 1
    with pytest.raises(ValueError):
        save_style(db, 1, (1.2, -0.2, 0.0), frontline=True)
    save_style(db, 3, None, frontline=None)  # confirm the seed as is
    confirmed = load_profiles(db, "16.21").style(3)
    assert confirmed.reviewed and confirmed.weights == (0.0, 0.0, 1.0) and confirmed.source == "seed"


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


# ---------- API ----------
def test_champion_styles_api(make_services, db):
    svc = make_services(db=db)
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        rows = {r["champ"]["id"]: r for r in c.get("/api/champions/styles").json()["champions"]}
        assert rows[1]["weights"] == {"front_to_back": 0.3, "dive": 0.7, "poke": 0.0} and rows[1]["source"] == "seed"
        assert rows[11]["unsure"] and rows[11]["source"] == "classes"
        put = c.put("/api/champions/styles/1", json={"weights": {"front_to_back": 0.5, "dive": 0.5, "poke": 0.0},
                                                      "frontline": True})
        assert put.status_code == 200 and put.json()["source"] == "user"
        bad = c.put("/api/champions/styles/1", json={"weights": {"front_to_back": 0.9, "dive": 0.5, "poke": 0.0},
                                                      "frontline": True})
        assert bad.status_code == 422
        assert c.delete("/api/champions/styles/1").json()["source"] == "seed"
        body = c.get("/api/champions/styles").json()
        assert body["reviewed"] == 0 and body["total"] == len(body["champions"])
