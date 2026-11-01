import pytest
from sqlmodel import Session, select

from draftassist.data.pools import (
    import_pools,
    list_players,
    load_team,
    parse_pool_text,
    resolve_from_session,
    set_players,
)
from draftassist.db.models import Champion, Player, PoolEntry
from draftassist.engine.model import tier_comfort
from draftassist.lcu.parser import parse_session

CHAMPS = {897: ("KSante", "K'Sante"), 68: ("Rumble", "Rumble"), 150: ("Gnar", "Gnar"), 113: ("Sejuani", "Sejuani"),
          62: ("MonkeyKing", "Wukong"), 145: ("Kaisa", "Kai'Sa"), 111: ("Nautilus", "Nautilus"),
          268: ("Azir", "Azir"), 61: ("Orianna", "Orianna"), 20: ("Nunu", "Nunu & Willump")}

SPEC_EXAMPLE = """TOP
K'Sante: main
Rumble: comfortable
Gnar: learning

JUNGLE
Sejuani: main
"""


@pytest.fixture
def db(engine, session):
    for cid, (key, name) in CHAMPS.items():
        session.add(Champion(champ_id=cid, key=key, name=name, patch="16.21.1"))
    session.commit()
    return engine


def test_parse_spec_example():
    parsed = parse_pool_text(SPEC_EXAMPLE, CHAMPS)
    assert parsed.ok
    assert parsed.entries == {"top": [(897, "main"), (68, "comfortable"), (150, "learning")], "jungle": [(113, "main")]}


def test_parse_is_forgiving_about_names_and_role_aliases():
    text = "middle\nazir: MAIN\n\nBOT\nkaisa: comfortable\n\nSupp:\nnautilus : learning\n\nJG\nwukong: main\nnunu willump: learning"
    parsed = parse_pool_text(text, CHAMPS)
    assert parsed.ok, parsed.errors
    assert parsed.entries == {"mid": [(268, "main")], "adc": [(145, "comfortable")], "support": [(111, "learning")],
                              "jungle": [(62, "main"), (20, "learning")]}


def test_parse_collects_every_error():
    text = "K'Sante: main\nTOPP\nTOP\nKsantee: main\nRumble: best\nGnar: main\nGnar: learning\nTOP\n"
    parsed = parse_pool_text(text, CHAMPS)
    assert not parsed.ok
    joined = "\n".join(parsed.errors)
    assert "line 1" in joined and "before a role header" in joined
    assert "'TOPP' is not a role" in joined
    assert "unknown champion 'Ksantee' (did you mean K'Sante?)" in joined
    assert "tier 'best'" in joined
    assert "Gnar is listed twice" in joined
    assert "TOP appears twice" in joined
    assert parse_pool_text("", CHAMPS).errors == ["nothing to import"]


def test_import_creates_placeholder_players_and_builds_team(db):
    counts = import_pools(db, parse_pool_text(SPEC_EXAMPLE, CHAMPS))
    assert counts == {"top": 3, "jungle": 1}
    players = {p["role"]: p for p in list_players(db)}
    assert players["top"]["resolved"] is False and players["jungle"]["bracket"] == "emerald"
    team = load_team(db)
    assert team.entry(897, "top").tier == "main" and team.entry(897, "top").comfort == pytest.approx(tier_comfort("main"))
    assert team.brackets["support"] == "iron"
    assert team.pools["mid"] == {}


def test_reimport_replaces_role_but_keeps_lock_and_history(db):
    import_pools(db, parse_pool_text(SPEC_EXAMPLE, CHAMPS))
    with Session(db) as s:
        e = s.exec(select(PoolEntry).where(PoolEntry.champ_id == 897)).one()
        e.tier_locked, e.scored_tier, e.games = True, "comfortable", 12
        s.add(e)
        s.commit()
    import_pools(db, parse_pool_text("TOP\nK'Sante: learning\nGnar: main", CHAMPS))
    with Session(db) as s:
        rows = {e.champ_id: e for e in s.exec(select(PoolEntry).where(PoolEntry.role == "top")).all()}
        assert set(rows) == {897, 150}  # Rumble dropped
        assert rows[897].manual_tier == "learning" and rows[897].tier_locked and rows[897].games == 12
        assert len(s.exec(select(PoolEntry).where(PoolEntry.role == "jungle")).all()) == 1  # untouched


def test_set_players_resolves_and_carries_pools(db):
    import_pools(db, parse_pool_text(SPEC_EXAMPLE, CHAMPS))
    resolver = {"Top Guy#EUW": "puuid-top"}.get
    stored = set_players(db, [{"role": "top", "riot_id": "Top Guy#EUW", "rank": "Gold II"},
                              {"role": "support", "riot_id": "Sup#EUW", "rank": "Iron I"}], resolver)
    by_role = {p["role"]: p for p in stored}
    assert by_role["top"]["resolved"] and by_role["top"]["rank"] == "Gold II"
    assert not by_role["support"]["resolved"]
    with Session(db) as s:
        assert {e.puuid for e in s.exec(select(PoolEntry).where(PoolEntry.role == "top")).all()} == {"puuid-top"}
        assert s.get(Player, "unresolved:top") is None
    with pytest.raises(ValueError, match="Name#TAG"):
        set_players(db, [{"role": "mid", "riot_id": "no tag"}])


def test_resolver_failure_leaves_player_unresolved(db):
    def broken(_):
        raise RuntimeError("401")

    stored = set_players(db, [{"role": "mid", "riot_id": "Mid#EUW"}], broken)
    assert next(p for p in stored if p["role"] == "mid")["resolved"] is False


def test_champ_select_resolves_players_and_supplies_roles(db):
    set_players(db, [{"role": "jungle", "riot_id": "OurJungle#EUW"}])
    members = [{"gameName": "OurJungle", "tagLine": "EUW", "puuid": "puuid-our-jungle"},
               {"gameName": "Random", "tagLine": "EUW", "puuid": "puuid-x"}]
    assert resolve_from_session(db, members) == 1
    with Session(db) as s:
        assert s.get(Player, "puuid-our-jungle").role == "jungle"

    import json
    from pathlib import Path

    session = json.loads((Path(__file__).resolve().parents[1] / "fixtures/lcu/sample-tournament/0001-session.json")
                         .read_text(encoding="utf-8"))
    for m in session["myTeam"]:
        m["assignedPosition"] = ""  # client gives no positions (e.g. custom game)
    live = parse_session(session, roles_by_puuid={"puuid-our-jungle": "jungle"})
    assert [s.role for s in live.our_slots] == [None, "jungle", None, None, None]
    assert live.our_slots[1].riot_id == "OurJungle#EUW"
