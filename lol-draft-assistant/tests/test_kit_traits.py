"""Stage 2 of docs/matchup-archetypes.md: kit-interaction traits, rated 0-3, for the Stage 3 matchup model."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from draftassist.data.tags import (
    TAGS_FILE,
    dump_tags_yaml,
    load_archetypes,
    load_kit,
    load_tags,
    load_tags_yaml,
    sync_tags,
    update_tag,
)
from draftassist.db.models import Champion
from draftassist.engine.traits import KIT_TAGS, TAGS, KitEntry, KitTraits

OUR_POOLS = {
    "top": ["Kayle", "Sion", "Jax", "Vayne", "Camille", "Ornn", "Aurora", "KSante"],
    "jungle": ["Warwick", "Elise", "Shyvana", "Hecarim", "Khazix", "Zac", "Viego"],
    "mid": ["Ahri", "Viktor", "Syndra", "Yasuo"],
    "adc": ["Sivir", "Vayne", "Ziggs", "Kaisa", "Jhin", "Jinx", "Xayah", "Aphelios"],
    "support": ["Karma", "Leona", "Nautilus", "Seraphine", "Rakan"],
}
YAML_TEXT = """\
Shen:
  top: {engage: 2, archetype: tank, auto_denial: 3, extend_fight: 2, reviewed: true}
Jax:
  top: {dive: 2, archetype: skirmisher, auto_reliant: 3, auto_denial: 3, reviewed: false}
  jungle: {dive: 2, archetype: skirmisher, reviewed: false}
Fiora:
  top: {max_hp_damage: 3, reviewed: false}
Lulu:
  support: {enchanter: 3, reviewed: false}
"""


@pytest.fixture
def tag_db(engine):
    with Session(engine) as s:
        for cid, key in ((98, "Shen"), (24, "Jax"), (114, "Fiora"), (117, "Lulu")):
            s.add(Champion(champ_id=cid, key=key, name=key, patch="16.22.1"))
        s.commit()
    return engine


@pytest.fixture
def tag_file(tmp_path):
    path = tmp_path / "champion_tags.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    return path


def test_vocabulary_is_the_docs_and_separate_from_the_synergy_tags():
    assert set(KIT_TAGS) == {"auto_reliant", "auto_denial", "max_hp_damage", "hard_gap_close", "kite", "escape",
                             "extend_fight", "anti_dive", "sustain", "ability_range"}
    assert not set(KIT_TAGS) & set(TAGS)


def test_kit_tags_load_and_round_trip(tag_file):
    raw = load_tags_yaml(tag_file)
    assert raw["Shen"]["top"].kit == {"auto_denial": 3, "extend_fight": 2}
    assert "auto_denial" not in raw["Shen"]["top"].values
    assert raw["Lulu"]["support"].kit == {}
    dump_tags_yaml(raw, tag_file)
    first = tag_file.read_text(encoding="utf-8")
    assert "top: {engage: 2, archetype: tank, auto_denial: 3, extend_fight: 2, reviewed: true}" in first
    assert "  top: {max_hp_damage: 3, reviewed: false}" in first
    dump_tags_yaml(load_tags_yaml(tag_file), tag_file)
    assert tag_file.read_text(encoding="utf-8") == first


@pytest.mark.parametrize("line", ["top: {auto_denial: 4}", "top: {kite: -1}", "top: {kite: high}"])
def test_bad_kit_values_are_rejected(tmp_path, line):
    path = tmp_path / "bad.yaml"
    path.write_text(f"Jax:\n  {line}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_tags_yaml(path)


def test_kit_only_line_is_not_a_tag_rating(tag_db, tag_file):
    sync_tags(tag_db, tag_file)
    tags = load_tags(tag_db)
    assert (114, "top") not in tags, "kit only: must stay out of the synergy models"
    assert (98, "top") in tags
    assert set(tags[(98, "top")].values) == set(TAGS)
    assert (114, "top") not in load_archetypes(tag_db)


def test_load_kit_reads_the_table(tag_db, tag_file):
    sync_tags(tag_db, tag_file)
    kit = load_kit(tag_db)
    assert kit[(98, "top")].values["auto_denial"] == 3
    assert kit[(98, "top")].reviewed and not kit[(24, "top")].reviewed
    assert kit[(114, "top")].values["max_hp_damage"] == 3
    assert (24, "jungle") not in kit and (117, "support") not in kit  # no kit tags on the line


def test_unreviewed_kit_blends_halfway_towards_the_role_average():
    e = {(1, "top"): KitEntry({"sustain": 3}, reviewed=False), (2, "top"): KitEntry({"sustain": 0}, reviewed=True)}
    kit = KitTraits(e)
    assert kit.role_average("top", "sustain") == pytest.approx(0.5)
    assert kit.intensity(1, "top", "sustain") == pytest.approx(0.5 * 1.0 + 0.5 * 0.5)
    assert kit.intensity(2, "top", "sustain") == 0.0  # reviewed: as written


def test_kit_is_borrowed_from_another_role_and_unknown_is_none():
    kit = KitTraits({(1, "top"): KitEntry({"escape": 3}, reviewed=True)})
    assert kit.intensity(1, "mid", "escape") == 1.0  # a kit is about the champion, not the lane
    assert kit.intensity(2, "top", "escape") is None
    assert kit.intensity(1, "top", "kite") == 0.0  # rated champion, tag left out: 0
    with pytest.raises(KeyError):
        kit.intensity(1, "top", "engage")


def test_editing_keeps_the_kit_unless_given(tag_db, tag_file):
    sync_tags(tag_db, tag_file)
    update_tag(tag_db, tag_file, 98, "top", {"engage": 1}, reviewed=True)
    assert load_tags_yaml(tag_file)["Shen"]["top"].kit == {"auto_denial": 3, "extend_fight": 2}
    update_tag(tag_db, tag_file, 98, "top", {"engage": 1}, reviewed=True, kit={"sustain": 2, "kite": 0})
    assert load_tags_yaml(tag_file)["Shen"]["top"].kit == {"sustain": 2}  # 0 isn't stored
    with pytest.raises(ValueError):
        update_tag(tag_db, tag_file, 98, "top", {}, reviewed=True, kit={"wombo": 1})


# ---------- the shipped draft ----------
def test_shipped_file_keeps_the_same_tag_ratings():
    raw = load_tags_yaml(TAGS_FILE)
    assert sum(e.rated for by_role in raw.values() for e in by_role.values()) == 247


def test_shipped_kit_tags_are_valid_and_unreviewed():
    raw = load_tags_yaml(TAGS_FILE)
    n = 0
    for key, by_role in raw.items():
        for role, e in by_role.items():
            n += bool(e.kit)
            assert set(e.kit) <= set(KIT_TAGS), (key, role)
            assert all(1 <= v <= 3 for v in e.kit.values()), (key, role)
            assert not (e.kit and e.reviewed), (key, role)  # drafted by Claude: the user reviews them
    assert n >= 100


def test_our_pools_have_kit_tags():
    raw = load_tags_yaml(TAGS_FILE)
    gaps = [(r, k) for r, keys in OUR_POOLS.items() for k in keys if not raw.get(k, {}).get(r) or not raw[k][r].kit]
    assert gaps == [], f"pool champions without kit tags: {gaps}"


def test_the_docs_examples_are_seeded():
    raw = load_tags_yaml(TAGS_FILE)
    high = {  # the doc's examples read as defining: at least 2
        "auto_reliant": [("Jax", "top"), ("Irelia", "top"), ("Jinx", "adc")],
        "auto_denial": [("Shen", "top"), ("Malphite", "top"), ("Jax", "top")],
        "max_hp_damage": [("Fiora", "top"), ("Camille", "top"), ("Vayne", "adc")],
        "hard_gap_close": [("Irelia", "top"), ("Pantheon", "top"), ("Renekton", "top")],
        "kite": [("Gnar", "top"), ("Teemo", "top"), ("Vayne", "top"), ("Jayce", "top")],
        "escape": [("Kennen", "top"), ("Syndra", "mid")],
        "extend_fight": [("Darius", "top"), ("Shen", "top"), ("Mordekaiser", "top")],
        "anti_dive": [("Vex", "mid"), ("Lissandra", "mid"), ("Malzahar", "mid"), ("Annie", "mid")],
        "sustain": [("Vladimir", "mid"), ("Olaf", "top"), ("Aatrox", "top")],
        "ability_range": [("Ziggs", "adc"), ("Xerath", "mid"), ("Velkoz", "mid")],
    }
    for tag, champs in high.items():
        for key, role in champs:
            assert raw[key][role].kit.get(tag, 0) >= 2, (tag, key, role)
    assert raw["Cassiopeia"]["mid"].kit.get("ability_range", 0) <= 1  # the doc's low example


# ---------- Tags page API ----------
def test_tags_api_shows_and_edits_kit_tags(make_services, tag_db, tag_file):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    sync_tags(tag_db, tag_file)
    svc = make_services(db=tag_db)
    svc.tags_path = tag_file
    with TestClient(create_app(svc, stats_refresh_s=0)) as c:
        body = c.get("/api/tags").json()
        assert body["kit_tags"] == list(KIT_TAGS)
        rows = {r["champ_id"]: r for r in body["champions"]}
        assert rows[98]["kit"]["top"] == {"auto_denial": 3, "extend_fight": 2}
        assert rows[114]["kit"]["top"] == {"max_hp_damage": 3}
        assert "top" not in rows[114]["entries"]
        ok = c.put("/api/tags/98/top", json={"values": {"engage": 2}, "reviewed": True, "kit": {"sustain": 1}})
        assert ok.status_code == 200 and ok.json()["kit"]["top"] == {"sustain": 1}
        bad = c.put("/api/tags/98/top", json={"values": {}, "reviewed": True, "kit": {"sustain": 9}})
        assert bad.status_code == 422
        keep = c.put("/api/tags/98/top", json={"values": {"engage": 2}, "reviewed": True})
        assert keep.json()["kit"]["top"] == {"sustain": 1}
