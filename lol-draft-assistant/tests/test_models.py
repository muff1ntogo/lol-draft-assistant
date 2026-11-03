from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from draftassist.db.models import (
    Champion,
    ChampRoleStats,
    Draft,
    DraftAction,
    Player,
    PoolEntry,
)

EXPECTED_TABLES = {
    "champions", "champ_role_stats", "matchups", "synergy_pairs", "synergy_role_stats",
    "players", "pool_entries", "drafts", "draft_actions",
}


def test_all_tables_created(engine):
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


@pytest.mark.parametrize(
    ("table", "pk"),
    [
        ("champ_role_stats", ["patch", "bracket", "champ_id", "role"]),
        ("matchups", ["patch", "bracket", "role", "champ_a", "champ_b"]),
        ("synergy_pairs", ["window", "bracket", "role_a", "champ_a", "role_b", "champ_b"]),
        ("pool_entries", ["puuid", "champ_id", "role"]),
        ("draft_actions", ["draft_id", "seq"]),
    ],
)
def test_composite_primary_keys(engine, table, pk):
    assert inspect(engine).get_pk_constraint(table)["constrained_columns"] == pk


def test_champion_roundtrip_with_json_roles(session):
    session.add(Champion(champ_id=897, key="KSante", name="K'Sante", roles=["top"], patch="15.21.1"))
    session.commit()
    row = session.get(Champion, 897)
    assert row.roles == ["top"] and row.name == "K'Sante"


def test_stats_key_is_unique(session):
    key = dict(patch="15.21", bracket="gold", champ_id=266, role="top")
    session.add(ChampRoleStats(**key, games=10, wins=5))
    session.commit()
    session.add(ChampRoleStats(**key, games=1, wins=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_player_pool_and_draft_roundtrip(session):
    session.add(Player(puuid="p1", riot_id="Top#EUW", role="top", rank="Gold IV", bracket="gold"))
    session.add(PoolEntry(puuid="p1", champ_id=897, role="top", manual_tier="main"))
    draft = Draft(side="blue", played_at=datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc), queue_id=440)
    session.add(draft)
    session.commit()
    session.add(DraftAction(draft_id=draft.draft_id, seq=1, side="blue", type="ban", champ_id=0))
    session.commit()

    assert session.get(PoolEntry, ("p1", 897, "top")).manual_tier == "main"
    actions = session.exec(select(DraftAction).where(DraftAction.draft_id == draft.draft_id)).all()
    assert [(a.seq, a.type, a.champ_id) for a in actions] == [(1, "ban", 0)]


def test_pool_entry_requires_known_player(session):
    session.add(PoolEntry(puuid="ghost", champ_id=897, role="top", manual_tier="main"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_obsolete_role_less_synergy_table_is_dropped(tmp_path):
    from sqlalchemy import text

    from draftassist.db.session import get_engine, init_db

    old = get_engine(tmp_path / "old.sqlite")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE synergy (patch TEXT, bracket TEXT, champ_a INT, champ_b INT, games INT, wins INT)"))
    assert "synergy" not in inspect(init_db(tmp_path / "old.sqlite")).get_table_names()
