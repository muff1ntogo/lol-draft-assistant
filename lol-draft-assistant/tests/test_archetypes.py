"""Stage 1 of docs/matchup-archetypes.md: the archetype taxonomy per champion and role."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from draftassist.data.archetypes import (
    ARCHETYPES,
    Archetype,
    fallback_archetype,
    pool_gaps,
    resolve_archetype,
)
from draftassist.data.tags import (
    TAGS_FILE,
    dump_tags_yaml,
    load_archetypes,
    load_tags,
    load_tags_yaml,
    remove_tag,
    sync_tags,
    update_tag,
)
from draftassist.db.models import Champion
from draftassist.engine.model import ROLES

# Our five pools (Pools page, 4 Nov 2026). Keep in step when a pool changes: the database isn't in the repo.
OUR_POOLS = {
    "top": ["Kayle", "Sion", "Jax", "Vayne", "Camille", "Ornn", "Aurora", "KSante"],
    "jungle": ["Warwick", "Elise", "Shyvana", "Hecarim", "Khazix", "Zac", "Viego"],
    "mid": ["Ahri", "Viktor", "Syndra", "Yasuo"],
    "adc": ["Sivir", "Vayne", "Ziggs", "Kaisa", "Jhin", "Jinx", "Xayah", "Aphelios"],
    "support": ["Karma", "Leona", "Nautilus", "Seraphine", "Rakan"],
}
CLASSES = ["Assassin", "Fighter", "Mage", "Marksman", "Support", "Tank"]

YAML_TEXT = """\
Leona:
  support: {engage: 3, archetype: engage, source: video, reviewed: true}
Karthus:
  jungle: {hypercarry: 2, archetype: farmer, reviewed: false}
  adc: {archetype: mage_bot, source: video, reviewed: false}
Sett:
  top: {engage: 2, archetype: juggernaut, archetype2: diver, reviewed: false}
Lulu:
  support: {enchanter: 3, reviewed: false}
"""


@pytest.fixture
def tag_db(engine):
    with Session(engine) as s:
        for cid, key in ((89, "Leona"), (30, "Karthus"), (875, "Sett"), (117, "Lulu")):
            s.add(Champion(champ_id=cid, key=key, name=key, patch="16.21.1"))
        s.commit()
    return engine


@pytest.fixture
def tag_file(tmp_path):
    path = tmp_path / "champion_tags.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    return path


# ---------- test 1: every YAML archetype is in the vocabulary ----------
def test_vocabulary_has_every_role():
    assert set(ARCHETYPES) == set(ROLES)
    assert all(len(v) == 5 for v in ARCHETYPES.values())


def test_shipped_archetypes_are_in_the_vocabulary():
    raw = load_tags_yaml(TAGS_FILE)  # load validates; checked again here so a loader change can't hide it
    seen = 0
    for key, by_role in raw.items():
        for role, e in by_role.items():
            if e.archetype is None:
                continue
            seen += 1
            assert e.archetype in ARCHETYPES[role], (key, role, e.archetype)
            assert e.archetype2 is None or e.archetype2 in ARCHETYPES[role], (key, role, e.archetype2)
            assert e.archetype != e.archetype2, (key, role)
            assert not e.reviewed  # drafted by Claude: the user reviews them
    assert seen >= 250


@pytest.mark.parametrize("line", [
    "top: {archetype: wizard}",  # not a word in the vocabulary
    "top: {archetype: tempo_ganker}",  # a jungle word, not a top one
    "top: {archetype: tank, archetype2: wizard}",
    "top: {archetype: tank, archetype2: tank}",
    "top: {archetype2: tank}",  # a secondary needs a primary
    "top: {archetype: tank, source: blog}",
])
def test_bad_archetypes_are_rejected(tmp_path, line):
    path = tmp_path / "bad.yaml"
    path.write_text(f"Sett:\n  {line}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_tags_yaml(path)


# ---------- test 2: the fallback covers every champion in every role ----------
@pytest.mark.parametrize("role", ROLES)
def test_fallback_covers_every_class_and_no_class(role):
    for classes in ([c] for c in CLASSES):
        assert fallback_archetype(role, classes) in ARCHETYPES[role]
    assert fallback_archetype(role, []) in ARCHETYPES[role]
    assert fallback_archetype(role, None) in ARCHETYPES[role]
    assert fallback_archetype(role, ["Something new"]) in ARCHETYPES[role]


def test_fallback_uses_the_first_class_data_dragon_lists():
    assert fallback_archetype("top", ["Tank", "Fighter"]) == "tank"
    assert fallback_archetype("top", ["Marksman", "Fighter"]) == "ranged"
    assert fallback_archetype("jungle", ["Assassin", "Fighter"]) == "assassin"
    assert fallback_archetype("adc", ["Mage"]) == "mage_bot"
    assert fallback_archetype("support", ["Support", "Mage"]) == "enchanter"


def test_resolve_prefers_the_entry_and_flags_the_fallback():
    tagged = Archetype("diver", None, reviewed=True, source="video", untagged=False)
    entries = {(1, "top"): tagged}
    assert resolve_archetype(entries, 1, "top", ["Tank"]) is tagged
    other = resolve_archetype(entries, 1, "support", ["Tank"])  # archetypes are per role: no borrowing
    assert other.primary == "engage" and other.untagged and not other.reviewed
    assert other.weight == 0.5 and tagged.weight == 1.0


def test_fallback_covers_every_champion_role_in_real_use():
    # Every shipped entry resolves, tagged or not, for a champion with any classes.
    raw = load_tags_yaml(TAGS_FILE)
    for key, by_role in raw.items():
        for role in ROLES:
            got = resolve_archetype({}, key, role, ["Fighter"])
            assert got.primary in ARCHETYPES[role], (key, role)


# ---------- test 3: our five pools are fully tagged ----------
def test_our_pools_are_fully_tagged():
    gaps = pool_gaps(OUR_POOLS, load_tags_yaml(TAGS_FILE))
    assert gaps == [], f"pool champions without an archetype: {gaps}"


def test_pool_gaps_lists_what_is_missing(tag_file):
    raw = load_tags_yaml(tag_file)
    assert pool_gaps({"support": ["Leona", "Lulu"], "adc": ["Karthus", "Sett"]}, raw) == [
        ("support", "Lulu"), ("adc", "Sett")]


def test_video_seeds_are_marked_and_match_the_doc():
    raw = load_tags_yaml(TAGS_FILE)
    want = {
        ("Darius", "top"): "juggernaut", ("Sett", "top"): "juggernaut", ("Aatrox", "top"): "diver",
        ("Fiora", "top"): "skirmisher", ("Malphite", "top"): "tank", ("Varus", "top"): "ranged",
        ("Jinx", "adc"): "scaler", ("Draven", "adc"): "lane_bully", ("Ashe", "adc"): "poke",
        ("Kaisa", "adc"): "skirmisher", ("Karthus", "adc"): "mage_bot", ("LeeSin", "jungle"): "tempo_ganker",
        ("MasterYi", "jungle"): "farmer", ("Hecarim", "jungle"): "skirmisher", ("Nunu", "jungle"): "tank",
        ("Qiyana", "jungle"): "assassin", ("Xerath", "mid"): "artillery", ("Orianna", "mid"): "control_mage",
        ("Vladimir", "mid"): "battle_mage", ("Yone", "mid"): "skirmisher",
    }
    for (key, role), archetype in want.items():
        e = raw[key][role]
        assert (e.archetype, e.source) == (archetype, "video"), (key, role)
    assert raw["Sett"]["top"].archetype2 == "diver"
    assert raw["Nunu"]["jungle"].archetype2 == "tempo_ganker"
    assert raw["Chogath"]["jungle"].archetype2 == "farmer"


# ---------- storage: an archetype must not change anything that reads the 0-3 tags ----------
def test_archetype_only_entry_is_not_a_tag_entry(tag_db, tag_file):
    assert sync_tags(tag_db, tag_file)["loaded"] == 5
    tags = load_tags(tag_db)
    assert (30, "adc") not in tags, "archetype-only: must stay out of the synergy models"
    assert (30, "jungle") in tags and (117, "support") in tags
    arch = load_archetypes(tag_db)
    assert arch[(30, "adc")].primary == "mage_bot" and arch[(30, "adc")].source == "video"
    assert arch[(875, "top")].secondary == "diver"
    assert arch[(89, "support")].reviewed and arch[(89, "support")].weight == 1.0
    assert (117, "support") not in arch  # tags but no archetype: the fallback applies


def test_shipped_file_keeps_the_same_tag_values():
    # Adding archetypes must leave every 0-3 rating alone: 247 rated entries, as before.
    raw = load_tags_yaml(TAGS_FILE)
    assert sum(e.rated for by_role in raw.values() for e in by_role.values()) == 247


def test_yaml_round_trip_is_deterministic(tag_file):
    dump_tags_yaml(load_tags_yaml(tag_file), tag_file)
    first = tag_file.read_text(encoding="utf-8")
    assert "adc: {archetype: mage_bot, source: video, reviewed: false}" in first
    assert "top: {engage: 2, archetype: juggernaut, archetype2: diver, reviewed: false}" in first
    dump_tags_yaml(load_tags_yaml(tag_file), tag_file)
    assert tag_file.read_text(encoding="utf-8") == first


def test_editing_tags_keeps_the_archetype_and_its_source(tag_db, tag_file):
    sync_tags(tag_db, tag_file)
    update_tag(tag_db, tag_file, 89, "support", {"engage": 2}, reviewed=False)
    e = load_tags_yaml(tag_file)["Leona"]["support"]
    assert (e.values["engage"], e.archetype, e.source) == (2, "engage", "video")


def test_changing_an_archetype_clears_its_source_and_blank_clears_it(tag_db, tag_file):
    sync_tags(tag_db, tag_file)
    update_tag(tag_db, tag_file, 89, "support", {"engage": 3}, reviewed=True, archetype="catcher")
    e = load_tags_yaml(tag_file)["Leona"]["support"]
    assert (e.archetype, e.source) == ("catcher", None)
    update_tag(tag_db, tag_file, 875, "top", {"engage": 2}, reviewed=True, archetype2="")
    assert load_tags_yaml(tag_file)["Sett"]["top"].archetype2 is None
    update_tag(tag_db, tag_file, 875, "top", {"engage": 2}, reviewed=True, archetype="")
    e = load_tags_yaml(tag_file)["Sett"]["top"]
    assert e.archetype is None and e.archetype2 is None
    with pytest.raises(ValueError):
        update_tag(tag_db, tag_file, 89, "support", {"engage": 3}, reviewed=True, archetype="diver")


def test_removing_an_entry_removes_its_archetype(tag_db, tag_file):
    sync_tags(tag_db, tag_file)
    remove_tag(tag_db, tag_file, 30, "adc")
    assert (30, "adc") not in load_archetypes(tag_db)
    assert "adc" not in load_tags_yaml(tag_file)["Karthus"]


# ---------- Tags page API ----------
def test_tags_api_shows_and_edits_archetypes(make_services, tag_db, tag_file):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    with Session(tag_db) as s:
        s.get(Champion, 117).roles = ["support"]
        s.get(Champion, 117).tags = ["Support", "Mage"]
        s.commit()
    sync_tags(tag_db, tag_file)
    svc = make_services(db=tag_db)
    svc.tags_path = tag_file
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        body = c.get("/api/tags").json()
        assert body["archetypes"]["top"] == list(ARCHETYPES["top"])
        rows = {r["champ_id"]: r for r in body["champions"]}
        assert rows[875]["archetypes"]["top"] == {
            "archetype": "juggernaut", "archetype2": "diver", "source": None, "reviewed": False, "untagged": False}
        assert rows[30]["archetypes"]["adc"]["archetype"] == "mage_bot"
        assert "adc" not in rows[30]["entries"]  # archetype-only: no tag row
        assert rows[117]["archetypes"]["support"] == {  # no archetype in the file: the fallback, flagged
            "archetype": "enchanter", "archetype2": None, "source": None, "reviewed": False, "untagged": True}
        put = c.put("/api/tags/875/top", json={"values": {"engage": 2}, "reviewed": True, "archetype": "diver",
                                                "archetype2": ""})
        assert put.status_code == 200
        assert put.json()["archetypes"]["top"]["archetype"] == "diver"
        assert put.json()["archetypes"]["top"]["archetype2"] is None
        bad = c.put("/api/tags/875/top", json={"values": {}, "reviewed": True, "archetype": "farmer"})
        assert bad.status_code == 422
        # the old request body (no archetype) leaves it alone
        keep = c.put("/api/tags/875/top", json={"values": {"engage": 1}, "reviewed": True})
        assert keep.json()["archetypes"]["top"]["archetype"] == "diver"
