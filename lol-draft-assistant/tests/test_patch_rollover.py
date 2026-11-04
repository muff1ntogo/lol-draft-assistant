"""A new patch must not empty what's stable across patches (docs/matchup-archetypes.md, Decisions).

Synergy, traits, fits, damage profiles and timing curves pool every crawled patch up to the current one, older
ones decayed 0.5 ** (patches_ago / 6). Champion strength (win rates, matchups) stays on the current patch.
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select
from test_traits import BLUE, RED, match, store

from draftassist.data.aggregate import aggregate
from draftassist.data.profiles import load_profiles
from draftassist.data.synergy import crawl_weights, latest_window, load_damage, load_synergy
from draftassist.data.traits import load_traits
from draftassist.db.models import (
    BracketTotal,
    Champion,
    ChampRoleStats,
    Matchup,
    SynergyFit,
    SynergyRoleStats,
)

OLD, NEW = "16.21.712.4501", "16.22.718.1234"
DECAY = 0.5 ** (1 / 6)


@pytest.fixture
def db(engine):
    with Session(engine) as s:
        for c in BLUE + RED:
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch="16.22.1", attack_range=550))
        s.commit()
    for i in range(40):
        store(engine, match(f"EUW1_{i}", blue_win=i % 3 != 0, version=OLD))
    aggregate(engine, "16.21")
    return engine


def _games(db, window, champ=BLUE[0]):
    with Session(db) as s:
        return s.exec(select(SynergyRoleStats).where(SynergyRoleStats.window == window,
                                                     SynergyRoleStats.bracket == "all",
                                                     SynergyRoleStats.champ_id == champ)).one()


def test_weights_count_the_current_patch_even_with_no_matches_yet(db):
    with db.connect() as conn:
        assert crawl_weights(conn, "16.22") == pytest.approx({"16.22": 1.0, "16.21": DECAY})
        assert crawl_weights(conn, "16.21") == {"16.21": 1.0}  # never the future


def test_switching_patch_keeps_synergy_traits_and_fits(db):
    aggregate(db, "16.22")
    assert latest_window(db) == "16.22"
    row = _games(db, "16.22")
    assert row.games == 40 and row.games_w == pytest.approx(40 * DECAY)
    assert load_synergy(db).pairs  # the window the engine reads has the old patch's pairs
    measured = load_traits(db).measured[(BLUE[0], "top")]
    assert measured["games"] == 40 and measured["cc"] is not None
    with Session(db) as s:
        side = s.exec(select(SynergyFit).where(SynergyFit.window == "16.22", SynergyFit.model == "team_side")).one()
    assert side.support == 40  # the team model was fitted on the pooled matches, not on an empty patch


def test_new_matches_count_in_full(db):
    for i in range(10):
        store(db, match(f"EUW1_new{i}", version=NEW))
    aggregate(db, "16.22")
    row = _games(db, "16.22")
    assert row.games == 50 and row.games_w == pytest.approx(40 * DECAY + 10)


def test_an_old_window_never_includes_a_newer_patch(db):
    store(db, match("EUW1_new", version=NEW))
    aggregate(db, "16.21")
    assert _games(db, "16.21").games == 40


def test_champion_strength_stays_on_the_current_patch(db):
    aggregate(db, "16.22")
    with Session(db) as s:
        assert not s.exec(select(ChampRoleStats).where(ChampRoleStats.patch == "16.22")).all()
        assert not s.exec(select(Matchup).where(Matchup.patch == "16.22")).all()
        assert s.get(BracketTotal, ("16.22", "gold")) is None
        assert s.exec(select(ChampRoleStats).where(ChampRoleStats.patch == "16.21")).all()  # kept, unread


def test_damage_and_timing_carry_over_to_a_patch_with_no_matches(db):
    assert load_damage(db, "16.22")[(BLUE[0], "top")] == pytest.approx(load_damage(db, "16.21")[(BLUE[0], "top")])
    old = load_profiles(db, "16.21").curve(BLUE[0], "top")
    new = load_profiles(db, "16.22").curve(BLUE[0], "top")
    assert new is not None and new.curve == pytest.approx(old.curve)


def test_damage_weights_the_newer_patch_more(db):
    raw = match("EUW1_shift", version=NEW)
    for p in raw["info"]["participants"]:
        p["physicalDamageDealtToChampions"], p["magicDamageDealtToChampions"] = 0, 15_000
    for i in range(40):
        store(db, {**raw, "metadata": {"matchId": f"EUW1_shift{i}"}})
    aggregate(db, "16.22")
    phys, magic, _ = load_damage(db, "16.22")[(BLUE[0], "top")]
    assert magic > phys  # equal games: the newer patch's all-magic games outweigh the older mostly physical ones
    with db.connect() as conn:
        w = crawl_weights(conn, "16.22")
    share = 10_000 / 16_000
    assert phys == pytest.approx(share * w["16.21"] / (w["16.21"] + w["16.22"]))
