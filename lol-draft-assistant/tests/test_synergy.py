"""Synergy visualiser, Stage A: role-aware duo data, the normalised synergy score, trios, damage profile.
See docs/synergy-visualizer.md."""
import json
import random

import pytest
from sqlmodel import Session, select

from draftassist.data.aggregate import aggregate
from draftassist.data.backfill import backfill_damage
from draftassist.data.crawler import parse_match
from draftassist.data.riot import cache_path
from draftassist.data.settings_store import get_synergy_bracket, save_synergy_bracket
from draftassist.data.synergy import load_synergy, window_weights
from draftassist.db.models import (
    Champion,
    ChampRoleStats,
    CrawledMatch,
    MatchParticipant,
    SynergyPair,
    SynergyRoleStats,
)
from draftassist.engine.model import logit, sigmoid
from draftassist.engine.synergy import LOW_DATA_GAMES, pair_score, to_pp

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
BLUE = (1, 2, 3, 4, 5)
RED = (11, 12, 13, 14, 15)
# per role: physical, magic, true damage to champions
DAMAGE = ((20_000, 2_000, 1_000), (8_000, 6_000, 3_000), (1_000, 25_000, 500), (24_000, 1_000, 2_000), (500, 6_000, 0))


def match(match_id, blue=BLUE, red=RED, blue_win=True, version="16.21.712.4501", damage=True, bracket_seed=0):
    participants = []
    for team, champs, win in ((100, blue, blue_win), (200, red, not blue_win)):
        for champ, pos, (phys, magic, true) in zip(champs, POSITIONS, DAMAGE):
            p = {"championId": champ, "teamId": team, "teamPosition": pos, "win": win}
            if damage:
                p |= {"physicalDamageDealtToChampions": phys + bracket_seed, "magicDamageDealtToChampions": magic,
                      "trueDamageDealtToChampions": true}
            participants.append(p)
    return {"metadata": {"matchId": match_id},
            "info": {"gameVersion": version, "queueId": 420, "gameDuration": 1800,
                     "gameStartTimestamp": 1_793_000_000_000, "participants": participants,
                     "teams": [{"teamId": 100, "bans": []}, {"teamId": 200, "bans": []}]}}


def store(db, raw, bracket="gold"):
    """What Crawler._store does, without the Riot client."""
    from draftassist.data.crawler import Crawler

    c = Crawler.__new__(Crawler)
    c.db = db
    c._store(parse_match(raw), bracket)


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        for c in BLUE + RED:
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.21.1"))
        s.commit()
    return engine


# ---------- A3: the pair score ----------
def wins_for(p_a, p_b, s_true, games):
    return sigmoid(logit(p_a) + logit(p_b) + s_true) * games


def test_no_interaction_scores_zero():
    p_a, p_b = 0.53, 0.48
    games = 20_000
    assert pair_score(wins_for(p_a, p_b, 0.0, games), games, p_a, p_b).s == pytest.approx(0.0, abs=1e-9)
    # a duo of two strong champions that wins exactly as much as their strength predicts is not synergy
    assert pair_score(wins_for(0.55, 0.55, 0.0, games), games, 0.55, 0.55).s == pytest.approx(0.0, abs=1e-9)


def test_strong_interaction_survives_many_games_and_shrinks_on_few():
    p_a, p_b, true_s = 0.51, 0.50, 0.08
    big = pair_score(wins_for(p_a, p_b, true_s, 100_000), 100_000, p_a, p_b)
    assert big.s > 0
    assert big.s_raw == pytest.approx(true_s, abs=0.005)
    assert big.s == pytest.approx(big.s_raw, abs=0.005)  # the soft cap barely bites at +2pp
    small = pair_score(wins_for(p_a, p_b, true_s, 5), 5, p_a, p_b)
    assert 0 <= small.s < 0.05 * big.s


def test_symmetry_and_soft_cap():
    rng = random.Random(7)
    for _ in range(2_000):
        p_a, p_b = rng.uniform(0.3, 0.7), rng.uniform(0.3, 0.7)
        games = rng.choice((0, 1, 10, 100, 1_000, 100_000))
        wins = rng.uniform(0, games)
        ab, ba = pair_score(wins, games, p_a, p_b), pair_score(wins, games, p_b, p_a)
        assert ab.s == pytest.approx(ba.s, abs=1e-12)
        assert abs(to_pp(ab.s)) <= 7.5
    # the most extreme possible duo still lands at the cap, not beyond
    assert to_pp(pair_score(1e6, 1e6, 0.5, 0.5).s) == pytest.approx(7.5, abs=0.01)
    assert to_pp(pair_score(0, 1e6, 0.5, 0.5).s) == pytest.approx(-7.5, abs=0.01)
    assert pair_score(0, 0, 0.6, 0.4).s == 0.0


def test_low_data_flag():
    assert pair_score(15, LOW_DATA_GAMES - 1, 0.5, 0.5).low_data
    assert not pair_score(15, LOW_DATA_GAMES, 0.5, 0.5).low_data


# ---------- A4: trios ----------


# ---------- A2: pooling window ----------
def test_window_weights_decay_by_patches_ago():
    w = window_weights(["16.15", "16.21", "16.20", "15.24"], current="16.21")
    assert w["16.21"] == 1.0
    assert w["16.20"] == pytest.approx(0.5 ** (1 / 6))
    assert w["16.15"] == pytest.approx(0.5 ** (2 / 6))
    assert w["15.24"] == pytest.approx(0.5 ** (3 / 6))
    assert window_weights(["16.22", "16.21"], current="16.21") == {"16.21": 1.0}  # never the future
    assert window_weights(["16.21"], current="16.21") == {"16.21": 1.0}


# ---------- A1: collection and aggregation ----------
def test_parse_match_reads_damage():
    m = parse_match(match("EUW1_1"))
    assert m.damage[0] == DAMAGE[0]
    assert len(m.damage) == len(m.participants) == 10
    assert all(d == (None, None, None) for d in parse_match(match("EUW1_2", damage=False)).damage)


def test_synergy_pairs_are_role_aware_and_stored_once(db):
    store(db, match("EUW1_1", blue_win=True))
    store(db, match("EUW1_2", blue_win=False))
    store(db, match("EUW1_3", blue_win=True), bracket="iron")
    aggregate(db, "16.21")
    with Session(db) as s:
        pairs = s.exec(select(SynergyPair).where(SynergyPair.window == "16.21", SynergyPair.bracket == "gold")).all()
        assert len(pairs) == 20  # 10 per team
        for p in pairs:
            assert (p.role_a, p.champ_a) < (p.role_b, p.champ_b)
        # jungle (2) + top (1) on blue: ordered by (role, champ), so jungle comes first
        jt = s.get(SynergyPair, ("16.21", "gold", "jungle", 2, "top", 1))
        assert (jt.games, jt.games_w, jt.wins_w) == (2, 2.0, 1.0)
        pooled = s.get(SynergyPair, ("16.21", "all", "jungle", 2, "top", 1))
        assert (pooled.games, pooled.wins_w) == (3, 2.0)
        role = s.get(SynergyRoleStats, ("16.21", "all", 1, "top"))
        assert (role.games, role.games_w, role.wins_w) == (3, 3.0, 2.0)
        assert s.get(SynergyRoleStats, ("16.21", "iron", 1, "top")).games == 1

    aggregate(db, "16.21")  # idempotent
    with Session(db) as s:
        assert s.get(SynergyPair, ("16.21", "all", "jungle", 2, "top", 1)).games == 3


def test_damage_shares_sum_to_one_per_champion_and_role(db):
    store(db, match("EUW1_1"))
    store(db, match("EUW1_2", bracket_seed=5_000))
    store(db, match("EUW1_3", damage=False))  # unknown damage is left out of the mean, not counted as 0
    aggregate(db, "16.21")
    with Session(db) as s:
        rows = s.exec(select(ChampRoleStats).where(ChampRoleStats.patch == "16.21")).all()
        assert len(rows) == 10
        for r in rows:
            assert r.dmg_phys + r.dmg_magic + r.dmg_true == pytest.approx(1.0, abs=1e-9)
        top = s.get(ChampRoleStats, ("16.21", "gold", 1, "top"))
        expected = (20_000 / 23_000 + 25_000 / 28_000) / 2
        assert top.dmg_phys == pytest.approx(expected)
        mid = s.get(ChampRoleStats, ("16.21", "gold", 3, "mid"))
        assert mid.dmg_magic > 0.8


def test_load_synergy_scores_pairs_in_either_order(db):
    for i in range(40):
        store(db, match(f"EUW1_{i}", blue_win=i % 4 != 0))  # blue wins 75%
    aggregate(db, "16.21")
    table = load_synergy(db, "16.21")
    ab = table.pair("top", 1, "jungle", 2)
    assert ab == table.pair("jungle", 2, "top", 1)
    assert ab.games == 40 and not ab.low_data
    p = (30 + 25) / (40 + 50)  # 30/40 wins, shrunk towards 50% with k = 50
    assert table.strength(1, "top") == table.strength(2, "jungle") == pytest.approx(p)
    assert ab.s == pytest.approx(pair_score(30, 40, p, p).s)
    missing = table.pair("top", 1, "mid", 13)
    assert missing.s == 0.0 and missing.games == 0 and missing.low_data


def test_load_synergy_single_bracket(db):
    store(db, match("EUW1_1"))
    store(db, match("EUW1_2"), bracket="iron")
    aggregate(db, "16.21")
    assert load_synergy(db, "16.21", "iron").pair("top", 1, "jungle", 2).games == 1
    assert load_synergy(db, "16.21").pair("top", 1, "jungle", 2).games == 2


def test_synergy_bracket_setting(engine):
    assert get_synergy_bracket(engine) == "all"
    save_synergy_bracket(engine, "emerald")
    assert get_synergy_bracket(engine) == "emerald"
    with pytest.raises(ValueError):
        save_synergy_bracket(engine, "challenger-ish")


# ---------- damage backfill from the on-disk Riot cache ----------
def write_cache(cache_dir, raw):
    path = cache_path(cache_dir / "riot", "europe", f"/lol/match/v5/matches/{raw['metadata']['matchId']}", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": 0, "body": raw}), encoding="utf-8")


def test_backfill_damage_from_cache_is_idempotent(db, tmp_path):
    store(db, match("EUW1_1", damage=False))
    store(db, match("EUW1_2", damage=False))
    write_cache(tmp_path, match("EUW1_1"))  # EUW1_2 is not cached
    assert backfill_damage(db, tmp_path) == {"updated": 1, "missing": 1}
    assert backfill_damage(db, tmp_path) == {"updated": 0, "missing": 1}
    with Session(db) as s:
        p = s.get(MatchParticipant, ("EUW1_1", 1))
        assert (p.dmg_phys, p.dmg_magic, p.dmg_true) == DAMAGE[0]
        assert s.get(MatchParticipant, ("EUW1_2", 1)).dmg_phys is None
        assert len(s.exec(select(CrawledMatch)).all()) == 2
