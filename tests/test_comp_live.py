"""docs/comp-style.md Stage B: comp indicator for both teams, champion badges, 5 / 3 suggestion cards."""
import random
import time
from dataclasses import replace

import pytest
from test_engine_synergy import (
    AZIR,
    BANS,
    CATALOG,
    GNAR,
    KAISA,
    KSANTE,
    NAUTILUS,
    ORIANNA,
    RAKAN,
    RELL,
    RUMBLE,
    SEJUANI,
    TEAM,
    VARUS,
    VI,
    data,
    live,
    state,
)

from draftassist.api.live import build_live_view
from draftassist.api.prep_view import build_prep_view
from draftassist.engine.comp import (
    ChampionProfiles,
    ChampStyle,
    TimingCurve,
    badge,
    card_count,
    comp_effect,
    team_comp,
)
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import SKIPPED, DraftState
from draftassist.lcu.parser import Slot


def style(ftb, dive, poke, frontline=False):
    """Old three-style fixtures as identities: front to back -> protect, dive -> engage, poke -> poke."""
    return ChampStyle((dive, poke, 0.0, ftb, 0.0), frontline, "tags", False)


STYLES = {
    KSANTE: style(0.6, 0.4, 0.0, True), GNAR: style(0.6, 0.2, 0.2, True), RUMBLE: style(0.5, 0.0, 0.5),
    SEJUANI: style(0.5, 0.5, 0.0, True), VI: style(0.0, 1.0, 0.0), AZIR: style(0.6, 0.0, 0.4),
    ORIANNA: style(0.7, 0.0, 0.3), KAISA: style(0.3, 0.7, 0.0), VARUS: style(0.3, 0.0, 0.7),
    NAUTILUS: style(0.5, 0.5, 0.0, True), RAKAN: style(0.3, 0.7, 0.0), RELL: style(0.4, 0.6, 0.0, True),
}
CURVES = {
    (KSANTE, "top"): TimingCurve(0.30, 200), (SEJUANI, "jungle"): TimingCurve(-0.30, 300),
    (VI, "jungle"): TimingCurve(-0.40, 250), (AZIR, "mid"): TimingCurve(0.40, 150),
    (KAISA, "adc"): TimingCurve(0.02, 400), (NAUTILUS, "support"): TimingCurve(-0.20, 300),
    (RUMBLE, "top"): TimingCurve(-0.5, 20),  # too few games for a label
    (RUMBLE, "mid"): TimingCurve(0.5, 90),
}
PROFILES = ChampionProfiles(STYLES, CURVES)


def engine():
    return DraftEngine(replace(data(), profiles=PROFILES), TEAM)


# ---------- badges ----------
def test_badges_show_timing_main_style_and_front_line():
    assert badge(PROFILES, KSANTE, "top").text() == "Late · Engage / Protect · Front-line"  # 0.4 engage counts
    assert badge(PROFILES, VI, "jungle").text() == "Early · Engage"
    assert badge(PROFILES, KAISA, "adc").text() == "Engage"  # mid timing isn't shown on a badge
    assert badge(PROFILES, KAISA, "adc").timing == "mid"
    assert badge(PROFILES, RUMBLE, "top").timing is None  # 20 games: no label
    assert badge(PROFILES, RUMBLE, None).timing == "late"  # role unknown: the role with most games
    split = badge(PROFILES, SEJUANI, "jungle")  # 0.5 / 0.5: both named, no single main style
    assert split.text() == "Early · Engage / Protect · Front-line" and split.style is None
    assert badge(PROFILES, 999, "top").as_dict() == {"timing": None, "style": None, "styles": [], "frontline": False,
                                                     "text": ""}


# ---------- team comp ----------
def test_team_comp_converges_as_picks_are_added():
    empty = team_comp(PROFILES, [])
    assert (empty.style, empty.timing, empty.confidence) == ("mixed", None, 0)
    one = team_comp(PROFILES, [(VI, "jungle")])
    assert one.style == "mixed" and one.timing == "early"  # one champion doesn't make a style
    two = team_comp(PROFILES, [(VI, "jungle"), (RAKAN, "support")])
    assert two.style == "engage" and two.shares["engage"] == pytest.approx(0.85)
    three = team_comp(PROFILES, [(VI, "jungle"), (RAKAN, "support"), (KSANTE, "top")])
    assert three.style == "engage" and three.confidence == pytest.approx(0.6) and three.frontliners == 1
    assert three.timing == "mid"  # (-0.40 + 0.30) / 2 = -0.05: Rakan has no curve
    d = three.as_dict()
    assert d["timing_label"] == "Mix" and d["style_label"] == "Engage" and d["picks"] == 3


def test_a_style_needs_two_contributors_and_no_front_liner():
    # merged identities: Protect no longer needs a front-liner (docs/synergy-v2.md, S7-3)
    two = team_comp(PROFILES, [(AZIR, "mid"), (ORIANNA, "mid")])
    assert two.shares["protect"] == pytest.approx(0.65) and two.style == "protect"
    assert team_comp(PROFILES, [(AZIR, "mid")]).style == "mixed"


def test_comp_effect_for_suggestion_cards():
    assert comp_effect(PROFILES, [(VI, "jungle")], RAKAN, "support") == ["completes Engage"]
    assert comp_effect(PROFILES, [(VI, "jungle"), (RAKAN, "support")], KAISA, "adc") == ["fits Engage"]
    # Sejuani is half front to back, half dive: both styles on the badge, so it fits Engage
    assert comp_effect(PROFILES, [(VI, "jungle"), (RAKAN, "support")], SEJUANI, "jungle") == ["fits Engage",
                                                                                              "pushes Early"]
    assert comp_effect(PROFILES, [(VI, "jungle"), (RAKAN, "support")], AZIR, "mid") == ["pushes Late"]
    assert comp_effect(PROFILES, [(AZIR, "mid")], ORIANNA, "mid") == ["completes Protect"]
    assert comp_effect(PROFILES, [(AZIR, "mid"), (ORIANNA, "mid")], GNAR, "top") == ["fits Protect"]
    assert comp_effect(PROFILES, [], AZIR, "mid") == ["pushes Late"]


# ---------- 5 / 3 cards ----------
def test_card_count_by_ban_phase_and_our_pick_number():
    assert card_count("ban", 1, 0) == 5 and card_count("ban", 2, 3) == 3
    assert [card_count("pick", 1, n) for n in range(5)] == [5, 5, 5, 3, 3]


PHASE1 = (KSANTE, GNAR, RELL, KAISA, NAUTILUS, VARUS)  # B1, R1, R2, B2, B3, R3


def test_live_view_shows_5_cards_early_and_3_late():
    eng = engine()
    first = build_live_view(eng, CATALOG, live(state()), "fixture", None)["center"]  # B ban 1
    assert first["mode"] == "our_ban" and len(first["cards"]) == 5
    pick1 = build_live_view(eng, CATALOG, live(state(*BANS)), "fixture", None)["center"]  # B1
    assert pick1["mode"] == "our_pick" and len(pick1["cards"]) == 5
    ban2 = build_live_view(eng, CATALOG, live(state(*BANS, *PHASE1, SKIPPED)), "fixture", None)["center"]
    assert ban2["mode"] == "our_ban" and 0 < len(ban2["cards"]) <= 3  # R bans first in phase 2, then us
    # (the 12-champion fixture has only 2 useful bans left: exactly 3 is shown on full-size data below)
    last = state(*BANS, *PHASE1, *(SKIPPED,) * 4, VI)  # R4 Vi: B4/B5 ours
    assert last.is_our_turn and last.step.kind == "pick" and len(last.our_picks) == 3
    view = build_live_view(eng, CATALOG, live(last), "fixture", None)
    assert len(view["center"]["cards"]) <= 3 and view["center"]["cards"]


def test_flex_counts_by_our_pick_number():
    ten = (SKIPPED,) * 10
    first = DraftState(STANDARD_RANKED, "blue", ten)
    assert len(build_live_view(engine(), CATALOG, live(first), "fixture", None)["center"]["cards"]) == 5
    fourth = DraftState(STANDARD_RANKED, "blue", (*ten, KSANTE, GNAR, VI, KAISA, NAUTILUS, VARUS, RELL))
    assert fourth.is_our_turn and len(fourth.our_picks) == 3
    assert len(build_live_view(engine(), CATALOG, live(fourth), "fixture", None)["center"]["cards"]) <= 3


# ---------- the live payload ----------
def test_live_view_has_comp_for_both_teams_and_badges():
    eng = engine()
    st = state(*BANS, VI, KSANTE, GNAR, RAKAN)  # ours: Vi, Rakan; theirs: K'Sante, Gnar; our B3 next
    slots = [Slot(1, "jungle", VI, 0), Slot(4, "support", RAKAN, 0)]
    view = build_live_view(eng, CATALOG, live(st, slots), "fixture", None)
    ours, theirs = view["comp"]["ours"], view["comp"]["theirs"]
    assert ours["style"] == "engage" and ours["picks"] == 2 and ours["confidence"] == pytest.approx(0.4)
    assert theirs["style"] == "protect" and theirs["frontliners"] == 2
    jungle = next(s for s in view["our_slots"] if s["role"] == "jungle")
    assert jungle["badge"]["text"] == "Early · Engage"
    assert all("badge" in s for s in view["their_slots"])
    cards = view["center"]["cards"]
    assert len(cards) == 5 and all("badge" in c and "effect" in c for c in cards)
    kaisa = next((c for c in cards if c["champ"]["id"] == KAISA), None)
    if kaisa is not None:
        assert kaisa["effect"] == ["fits Engage"]


def test_synergy_nodes_carry_badges():
    from draftassist.engine.style import StyleModel
    from draftassist.engine.synergy_v2 import SynergyEvaluator
    from draftassist.engine.team import TeamModel
    from draftassist.engine.traits import EMPTY_TRAITS

    ev = SynergyEvaluator(data().synergy, EMPTY_TRAITS, StyleModel([], {}), TeamModel([], EMPTY_TRAITS))
    view = build_prep_view(ev, CATALOG, TEAM, profiles=PROFILES)
    nodes = {n["id"]: n for col in view["columns"] for n in col["nodes"]}
    assert nodes[SEJUANI]["badge"]["text"] == "Early · Engage / Protect · Front-line"
    assert nodes[NAUTILUS]["badge"]["styles"] == ["engage", "protect"]


# ---------- guardrails and response time at 5 cards ----------
def test_guardrails_hold_at_five_cards_on_full_size_data():
    full, team = synthetic_data()
    eng = DraftEngine(full, team)
    rng = random.Random(11)
    champs = sorted(full.champions())
    for _ in range(30):
        n = rng.randrange(6, 16)
        actions = rng.sample(champs, n)
        st = DraftState(TOURNAMENT, rng.choice(("blue", "red")), tuple(actions))
        if st.step is None or not st.is_our_turn:
            continue
        sugs = eng.suggest_picks(st, k=5) if st.step.kind == "pick" else eng.suggest_bans(st, k=5)
        ids = [s.champ_id for s in sugs]
        assert len(ids) == len(set(ids)) <= 5
        assert not set(ids) & st.unavailable()


def test_five_card_views_stay_within_budget():
    full, team = synthetic_data()
    eng = DraftEngine(full, team)
    catalog = type(CATALOG)("16.21.1", {c: (str(c), str(c)) for c in full.names})
    rng = random.Random(3)
    others = [c for c in sorted(full.champions()) if c not in team.pool_champs()]
    pool = sorted(team.pool_champs())
    worst = 0.0
    for _ in range(10):
        bans = rng.sample(others, 6)
        b1, b2, b3 = rng.sample(pool, 3)
        r1, r2, r3, r_ban = rng.sample([c for c in others if c not in bans], 4)
        phase2_ban = DraftState(TOURNAMENT, "blue", (*bans, b1, r1, r2, b2, b3, r3, r_ban))
        assert phase2_ban.is_our_turn and phase2_ban.step.kind == "ban" and phase2_ban.step.phase == 2
        for st, n in ((DraftState(TOURNAMENT, "blue", ()), 5), (DraftState(TOURNAMENT, "blue", tuple(bans)), 5),
                      (phase2_ban, 3)):
            t0 = time.perf_counter()
            view = build_live_view(eng, catalog, live(st), "fixture", None)
            worst = max(worst, time.perf_counter() - t0)
            assert len(view["center"]["cards"]) == n
    assert worst < 0.200
