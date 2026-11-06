"""Stage 7 of docs/matchup-archetypes.md: the Matchup tree, coverage strip and Live reasons."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from draftassist.data.archetypes import ARCHETYPES, load_archetype_plans
from draftassist.engine.engine import DraftEngine
from draftassist.engine.matchup_tree import GAP_PP, _shortlist, chip, matchup_tree
from draftassist.engine.model import (
    ROLES,
    BracketStats,
    EngineConfig,
    EngineData,
    OurTeam,
    RoleStat,
)

BRACKETS = dict.fromkeys(ROLES, "gold")
TOP = ARCHETYPES["top"]


class _Explainer:
    def __init__(self, term=None):
        self.term = term

    def explain(self, role, a, b):
        return self.term


def _engine(wr, pool, labels, explainer=None, rates=None):
    champs = {c for k in wr for c in k[1:]} | set(pool) | set(labels_champs(labels))
    meta = {c: {"top": RoleStat(0.5, (rates or {}).get(c, 0.1))} for c in champs}
    names = {c: f"C{c}" for c in champs}
    return DraftEngine(EngineData({"gold": BracketStats(meta, wr)}, names, labels=labels, explainer=explainer),
                       OurTeam.from_tiers({"top": pool}, BRACKETS), EngineConfig(beta=0.0))


def labels_champs(labels):
    return [c for c, _ in labels]


LABELS = {(1, "top"): "ranged", (2, "top"): "tank", (10, "top"): "juggernaut", (11, "top"): "juggernaut", (20, "top"): "tank"}


def test_rows_are_the_enemy_archetypes_with_their_most_picked_champions():
    eng = _engine({("top", 1, 10): 0.55, ("top", 1, 11): 0.53}, {1: "main"}, LABELS, rates={10: 0.2, 11: 0.05})
    rows = {r["archetype"]: r for r in matchup_tree(eng, "top", TOP)}
    assert set(rows) == set(TOP)
    assert [c["name"] for c in rows["juggernaut"]["champs"]] == ["C10", "C11"]
    assert rows["diver"]["champs"] == [] and rows["diver"]["chip"] == "none" and not rows["diver"]["gap"]


def test_the_best_answer_is_weighted_by_pick_rate_and_comes_from_the_pool():
    wr = {("top", 1, 10): 0.60, ("top", 1, 11): 0.40, ("top", 2, 10): 0.51, ("top", 2, 11): 0.51}
    eng = _engine(wr, {1: "main", 2: "learning"}, LABELS, rates={10: 0.3, 11: 0.1})
    best = next(r for r in matchup_tree(eng, "top", TOP) if r["archetype"] == "juggernaut")["best"]
    assert best["name"] == "C1" and best["pp"] == pytest.approx(5.0)  # (10 * 3 - 10) / 4


def _answer_row(champ, pp, counter_pp=0.0, confidence=0.1, own_pp=0.0):
    return {"champ": champ, "pp": pp, "counter_pp": counter_pp, "confidence": confidence, "own_pp": own_pp, "counter": False}


def _champs(answers):
    return [a["champ"] for a in answers]


def test_the_pools_best_comes_first_when_it_covers_and_the_rest_are_from_outside():
    pool = [_answer_row(1, 0.5), _answer_row(2, 1.0)]
    outside = [_answer_row(5, 3.0), _answer_row(6, 2.0), _answer_row(7, 1.5)]
    assert _champs(_shortlist(pool, outside)) == [2, 5, 6]


def test_a_pool_that_does_not_cover_leaves_all_three_places_to_the_outside():
    outside = [_answer_row(5, 3.0), _answer_row(6, 2.0), _answer_row(7, 1.5), _answer_row(8, 1.0)]
    for pool in ([_answer_row(1, -0.5)], [_answer_row(1, 0.5, confidence=0.0)], []):  # thin, no games, nothing
        assert _champs(_shortlist(pool, outside)) == [5, 6, 7]


def test_with_no_one_outside_the_pool_fills_the_list():
    pool = [_answer_row(c, -0.5 * c) for c in (1, 2, 3, 4)]
    assert _champs(_shortlist(pool, [])) == [1, 2, 3]


def test_a_counter_takes_the_last_place_but_only_one_and_never_the_pools():
    outside = [_answer_row(5, 3.0), _answer_row(6, 2.0), _answer_row(7, 0.5, 0.8, own_pp=-2.0), _answer_row(8, 0.2, 0.6)]
    shown = _shortlist([_answer_row(1, 1.0)], outside)
    assert _champs(shown) == [1, 5, 7]
    assert [a["champ"] for a in shown if a["counter"]] == [7]


def test_a_counter_already_on_the_list_is_only_tagged():
    shown = _shortlist([_answer_row(1, 1.0, 0.9)], [_answer_row(5, 3.0), _answer_row(6, 2.0), _answer_row(7, 0.5, 0.6)])
    assert _champs(shown) == [1, 5, 6] and shown[0]["counter"] and not any(a["counter"] for a in shown[1:])


@pytest.mark.parametrize("why_not", [_answer_row(7, -0.5, 2.0), _answer_row(7, 0.5, 0.4), _answer_row(7, 0.5, 2.0, 0.0)],
                         ids=["still loses", "gain too small", "no games"])
def test_not_a_counter(why_not):
    shown = _shortlist([_answer_row(1, 1.0)], [_answer_row(5, 3.0), _answer_row(6, 2.0), why_not])
    assert _champs(shown) == [1, 5, 6] and not any(a["counter"] for a in shown)


def _row_with_games(wr, pool, labels, archetype="juggernaut"):
    eng = _engine(wr, pool, labels)
    meta = {c: {"top": RoleStat(0.5, 0.1)} for k in wr for c in k[1:]}
    stats = BracketStats(meta, wr, matchup_games={k: 400 for k in wr}, matchup_k={"top": 400.0})
    return next(r for r in matchup_tree(eng, "top", TOP, stats=stats) if r["archetype"] == archetype)


def test_answers_come_from_both_sides_of_the_pool_and_the_chip_stays_about_the_pool():
    labels = {(c, "top"): "ranged" for c in (1, 2, 3, 4)} | {(10, "top"): "juggernaut"}
    wr = {("top", 1, 10): 0.51, ("top", 2, 10): 0.60, ("top", 3, 10): 0.55, ("top", 4, 10): 0.50}
    row = _row_with_games(wr, {1: "main"}, labels)
    assert [(a["name"], a["in_pool"]) for a in row["answers"]] == [("C1", True), ("C2", False), ("C3", False)]
    assert row["best"]["name"] == "C1" and row["chip"] == "green"
    thin = _row_with_games({**wr, ("top", 1, 10): 0.49}, {1: "main"}, labels)
    assert [a["name"] for a in thin["answers"]] == ["C2", "C3", "C4"]  # the strong outsiders don't make it covered
    assert thin["best"]["name"] == "C1" and thin["chip"] == "amber" and not thin["gap"]


def test_a_weak_champion_who_beats_one_archetype_makes_the_list_as_its_counter():
    labels = {(c, "top"): "ranged" for c in (1, 2, 3, 4)} | {(10, "top"): "juggernaut", (20, "top"): "tank"}
    wr = {("top", 1, 10): 0.60, ("top", 2, 10): 0.59, ("top", 3, 10): 0.58, ("top", 4, 10): 0.52,
          ("top", 1, 20): 0.60, ("top", 2, 20): 0.59, ("top", 3, 20): 0.58, ("top", 4, 20): 0.40}
    eng = _engine(wr, {1: "main"}, labels)
    meta = {c: {"top": RoleStat(0.5, 0.1)} for c in (1, 2, 3, 4, 10, 20)}
    stats = BracketStats(meta, wr, matchup_games={k: 400 for k in wr}, matchup_k={"top": 400.0})
    row = next(r for r in matchup_tree(eng, "top", TOP, stats=stats) if r["archetype"] == "juggernaut")
    assert [a["name"] for a in row["answers"]] == ["C1", "C2", "C4"]
    weak = row["answers"][2]
    assert weak["counter"] and weak["own_pp"] == -4.0 and weak["counter_pp"] == 4.5  # +6 on its average, the pool's +1.5
    assert row["best"]["name"] == "C1" and not row["best"]["counter"]


def test_a_gap_is_flagged_below_the_line_and_chips_follow():
    assert GAP_PP == -1.5
    assert (chip(1.0), chip(-1.0), chip(-2.0), chip(None)) == ("green", "amber", "red", "red")
    wr = {("top", 1, 10): 0.47, ("top", 1, 11): 0.47}
    row = next(r for r in matchup_tree(_engine(wr, {1: "main"}, LABELS), "top", TOP) if r["archetype"] == "juggernaut")
    assert row["gap"] and row["chip"] == "red"
    assert [b["name"] for b in row["ban_candidates"]] == ["C10", "C11"]


def test_lane_game_gold_and_confidence_are_each_reported():
    meta = {c: {"top": RoleStat(0.5, 0.1)} for c in (1, 10)}
    stats = BracketStats(meta, {("top", 1, 10): 0.54}, strength_in_matchups=True, matchup_games={("top", 1, 10): 200},
                         matchup_k={"top": 200.0}, lane_gold={("top", 1, 10): -310.0},
                         lane_slope={(1, "top"): 0.4, (10, "top"): 0.4})
    eng = DraftEngine(EngineData({"gold": stats}, {1: "A", 10: "B"}, labels={(1, "top"): "ranged", (10, "top"): "juggernaut"}),
                      OurTeam.from_tiers({"top": {1: "main"}}, BRACKETS), EngineConfig(beta=0.0))
    best = next(r for r in matchup_tree(eng, "top", TOP) if r["archetype"] == "juggernaut")["best"]
    assert best["game_pp"] == 4.0 and best["gold"] == -310 and best["confidence"] == 0.5
    assert best["lane_pp"] < 0


def test_expectation_and_who_answers_come_from_the_fit():
    table = {("ranged", "juggernaut"): 0.8, ("tank", "juggernaut"): -0.4, ("diver", "juggernaut"): 0.2,
             ("skirmisher", "juggernaut"): 0.1}
    expected = lambda a, b: table.get((a, b), 0.0)
    eng = _engine({("top", 1, 10): 0.55}, {1: "main"}, LABELS)
    plans = {"juggernaut": "kite them"}
    row = next(r for r in matchup_tree(eng, "top", TOP, plans, expected) if r["archetype"] == "juggernaut")
    assert row["expectation"] == 0.8 and row["answering"] == ["ranged", "diver"] and row["plan"] == "kite them"


def test_an_empty_pool_is_all_gaps_where_the_archetype_is_played():
    eng = _engine({}, {}, LABELS)
    row = next(r for r in matchup_tree(eng, "top", TOP) if r["archetype"] == "juggernaut")
    assert row["best"] is None and row["gap"] and row["chip"] == "red"


def test_plans_load_and_every_line_is_a_real_archetype():
    plans = load_archetype_plans()
    assert plans["mid"]["battle_mage"] == "vs battle mage: out-range and abuse in lane"
    assert plans["mid"]["assassin"] == "vs assassin: battle mage or anti-dive"
    for role, lines in plans.items():
        assert set(lines) <= set(ARCHETYPES[role]), role


def test_bad_plan_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("top:\n  plans:\n    wizard: nope\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_archetype_plans(path)


# ---------- Live reasons ----------
def _live_engine(explainer, edge_wr=0.56):
    meta = {1: {"top": RoleStat(0.5, 0.1)}, 2: {"top": RoleStat(0.5, 0.1)}}
    stats = BracketStats(meta, {("top", 1, 2): edge_wr}, strength_in_matchups=True)
    labels = {(1, "top"): "ranged", (2, "top"): "juggernaut"}
    return stats, DraftEngine(EngineData({"gold": stats}, {1: "Kayle", 2: "Darius"}, labels=labels, explainer=explainer),
                              OurTeam.from_tiers({"top": {1: "main"}}, BRACKETS), EngineConfig(beta=0.0))


def test_reason_uses_the_archetype_when_it_is_the_biggest_term():
    stats, eng = _live_engine(_Explainer(("E:ranged>juggernaut", 0.1)))
    assert eng._matchup_term(stats, "top", 1, 2)[1].startswith("vs juggernaut (Darius): ranged top favoured: Kayle +")


def test_a_kit_exception_overrides_the_archetype_line():
    stats, eng = _live_engine(_Explainer(("K:auto_denial*auto_reliant", 0.1)))
    assert eng._matchup_term(stats, "top", 1, 2)[1].startswith("Kayle blocks Darius's autos")
    stats, eng = _live_engine(_Explainer(("K:max_hp_damage*arch:tank", 0.1)))
    assert eng._matchup_term(stats, "top", 1, 2)[1].startswith("Kayle shreds tanky Darius")


def test_a_small_or_opposite_explanation_keeps_the_plain_reason():
    stats, eng = _live_engine(_Explainer(None))
    assert eng._matchup_term(stats, "top", 1, 2)[1] == "counters their Darius"
    stats, eng = _live_engine(_Explainer(("E:ranged>juggernaut", 0.1)), edge_wr=0.44)
    assert eng._matchup_term(stats, "top", 1, 2)[1] == "counters their Darius"  # not favoured, whatever the labels say


def test_pick_cards_carry_the_archetype_badge():
    from draftassist.api.live import _archetype_of

    _, eng = _live_engine(None)
    assert _archetype_of(eng, 1, "top") == "ranged"
    assert _archetype_of(eng, 9, "top") is None
    assert _archetype_of(eng, 1, None) is None


# ---------- explaining a matchup from the fit ----------
def test_the_model_names_its_biggest_term(engine):
    from draftassist.data.matchup_model import MatchupModel
    from draftassist.engine.traits import KitEntry, KitTraits

    classes = {1: ["Tank"], 2: ["Fighter"]}
    kit = KitTraits({(1, "top"): KitEntry({"auto_denial": 3}, True), (2, "top"): KitEntry({"auto_reliant": 3}, True)})
    from draftassist.data.archetypes import Archetype

    written = {(1, "top"): Archetype("tank", None, True), (2, "top"): Archetype("juggernaut", None, True)}
    coefs = {"top": {"E:juggernaut>tank": 0.2, "K:auto_denial*auto_reliant": 0.3}}
    model = MatchupModel("16.21", coefs, {}, written, classes, kit)
    assert model.explain("top", 1, 2) == ("K:auto_denial*auto_reliant", pytest.approx(0.3))
    assert model.explain("top", 2, 1) == ("E:juggernaut>tank", pytest.approx(0.2))
    assert model.explain("top", 1, 99) is None
    assert model.labels_table()[(1, "top")] == "tank"
    assert model.expected("top", "juggernaut", "tank") == pytest.approx(5.0)
    assert model.expected("top", "tank", "juggernaut") == pytest.approx(-5.0)


# ---------- the API ----------
def test_matchup_tree_api(make_services, engine):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app
    from draftassist.db.models import Champion

    with Session(engine) as s:
        s.add(Champion(champ_id=1, key="A", name="A", patch="16.21.1"))
        s.commit()
    with TestClient(create_app(make_services(db=engine), stats_refresh_s=0)) as c:
        body = c.get("/api/prep/matchup-tree").json()
    assert [r["role"] for r in body["roles"]] == list(ROLES)
    first = body["roles"][0]
    assert {"role", "role_label", "bracket", "rows", "health"} <= set(first)
    assert [r["archetype"] for r in first["rows"]] == list(TOP)


# ---------- the tree reads every crawled patch ----------
def test_pooled_stats_use_the_older_patch_and_every_tier(engine):
    from test_lane_matchup import FILLER_BLUE, FILLER_RED, _store

    from draftassist.data.aggregate import aggregate
    from draftassist.data.matchup_model import load_matchup_model, pooled_stats
    from draftassist.db.models import Champion, ChampionTag

    with Session(engine) as s:
        for c in (1, 3, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.22.1", tags=["Fighter"]))
        s.add(ChampionTag(champ_id=1, role="top", archetype="ranged", reviewed_arch=True))
        s.add(ChampionTag(champ_id=3, role="top", archetype="juggernaut", reviewed_arch=True))
        s.commit()
        for i in range(300):
            _store(s, f"EUW1_g{i}", (1, *FILLER_BLUE), (3, *FILLER_RED), i % 5 != 0, patch="16.21", bracket="gold")
            _store(s, f"EUW1_i{i}", (1, *FILLER_BLUE), (3, *FILLER_RED), i % 5 != 0, patch="16.21", bracket="iron")
        s.commit()
    aggregate(engine, "16.21")
    aggregate(engine, "16.22")  # the current patch has no matches
    model = load_matchup_model(engine)
    stats = pooled_stats(engine, model, "gold")
    assert stats.matchup_games[("top", 1, 3)] == pytest.approx(600 * 0.5 ** (1 / 6), abs=1)  # both tiers, decayed
    assert stats.matchup_wr("top", 1, 3) > 0.6
    assert 0.5 < stats.matchup_confidence("top", 1, 3) < 1
    assert stats.meta[1]["top"].pick_rate == pytest.approx(1.0)  # from the player's tier


def test_an_answer_with_no_games_behind_it_is_neither_covered_nor_thin():
    assert chip(0.5, 0.0) == "grey" and chip(-0.5, 0.0) == "grey"
    assert chip(0.5, 0.01) == "green" and chip(-0.5, 0.01) == "amber"
    assert chip(-2.0, 0.0) == "red" and chip(None) == "red"  # a gap still warns


def _two_patch_db(engine):
    """16.21: 300 matches a tier, ours winning 80%. 16.22 has no matches yet."""
    from test_lane_matchup import FILLER_BLUE, FILLER_RED, _store

    from draftassist.data.aggregate import aggregate
    from draftassist.db.models import Champion, ChampionTag

    with Session(engine) as s:
        for c in (1, 3, *FILLER_BLUE, *FILLER_RED):
            s.add(Champion(champ_id=c, key=f"C{c}", name=f"C{c}", patch="16.22.1", tags=["Fighter"]))
        s.add(ChampionTag(champ_id=1, role="top", archetype="ranged", reviewed_arch=True))
        s.add(ChampionTag(champ_id=3, role="top", archetype="juggernaut", reviewed_arch=True))
        s.commit()
        for i in range(300):
            _store(s, f"EUW1_g{i}", (1, *FILLER_BLUE), (3, *FILLER_RED), i % 5 != 0, patch="16.21", bracket="gold")
        s.commit()
    aggregate(engine, "16.21")
    aggregate(engine, "16.22")


def test_a_shorter_half_life_discounts_older_patches_in_the_tree_only(engine):
    from draftassist.data.matchup_model import load_matchup_model, pooled_stats

    _two_patch_db(engine)
    default = load_matchup_model(engine)
    tuned = load_matchup_model(engine, half_life=1)
    equal = load_matchup_model(engine, half_life=0)
    assert pooled_stats(engine, default, "gold").matchup_games[("top", 1, 3)] == pytest.approx(300 * 0.5 ** (1 / 6), abs=1)
    assert pooled_stats(engine, tuned, "gold").matchup_games[("top", 1, 3)] == pytest.approx(150, abs=1)
    assert pooled_stats(engine, equal, "gold").matchup_games[("top", 1, 3)] == 300
    assert tuned._lane_pairs is not default._lane_pairs
    assert tuned.coefs("top") == default.coefs("top")  # the fit isn't touched


def test_the_half_life_setting_through_the_api(make_services, engine):
    from fastapi.testclient import TestClient

    from draftassist.api.main import create_app

    _two_patch_db(engine)
    with TestClient(create_app(make_services(db=engine), stats_refresh_s=0)) as c:
        before = c.get("/api/prep/matchup-tree").json()
        assert before["half_life"] == 0  # every game counts the same until told otherwise
        assert next(m for m in before["matches"] if m["patch"] == "16.21")["weight"] == 1.0
        assert c.put("/api/prep/matchup-tree/half-life", json={"half_life": 1}).status_code == 200
        after = c.get("/api/prep/matchup-tree").json()
        assert after["half_life"] == 1
        assert next(m for m in after["matches"] if m["patch"] == "16.21")["weight"] == pytest.approx(0.5)
        for bad in (0.05, 25):
            assert c.put("/api/prep/matchup-tree/half-life", json={"half_life": bad}).status_code == 422
        assert c.get("/api/prep/matchup-tree").json()["half_life"] == 1
        assert c.put("/api/prep/matchup-tree/half-life", json={"half_life": 0}).status_code == 200
        assert c.get("/api/prep/matchup-tree").json()["half_life"] == 0


def test_saving_other_settings_keeps_the_half_life(engine):
    from draftassist.data.settings_store import get_engine_settings, save_engine_settings

    save_engine_settings(engine, tree_patch_half_life=2)
    save_engine_settings(engine, alpha=1.0)
    assert get_engine_settings(engine)["tree_patch_half_life"] == 2


def test_the_tree_reads_the_stats_it_is_given():
    eng = _engine({("top", 1, 10): 0.5, ("top", 1, 11): 0.5}, {1: "main"}, LABELS)
    meta = {c: {"top": RoleStat(0.5, 0.1)} for c in (1, 10)}
    pooled = BracketStats(meta, {("top", 1, 10): 0.60}, matchup_games={("top", 1, 10): 400}, matchup_k={"top": 400.0})
    row = next(r for r in matchup_tree(eng, "top", TOP, stats=pooled) if r["archetype"] == "juggernaut")
    assert [c["name"] for c in row["champs"]] == ["C10"]  # 11 isn't in these stats
    assert row["best"]["pp"] == 10.0 and row["best"]["confidence"] == 0.5

