"""docs/comp-style.md Stage C: comp coherence in the pick score, plus varied "Next 2 picks" pairs and the
Team page on the same style labels."""
from dataclasses import replace

import pytest
from sqlmodel import Session

from draftassist.api.live import build_live_view
from draftassist.data.settings_store import engine_config, save_engine_settings, settings_payload
from draftassist.db.models import AppSetting
from draftassist.db.session import init_db
from draftassist.engine.comp import STYLE_LABELS, STYLES
from draftassist.engine.engine import _LO_TO_PROB, DraftEngine
from draftassist.engine.model import BracketStats, EngineConfig, EngineData, OurTeam
from draftassist.engine.state import SKIPPED
from draftassist.learning.style import STYLES as TEAM_STYLES
from draftassist.learning.style import classify, get_hand_style, set_hand_style
from test_comp_live import PHASE1, PROFILES, style
from test_engine_synergy import (
    AZIR, BANS, CATALOG, GNAR, KAISA, KSANTE, META, NAMES, NAUTILUS, ORIANNA, RAKAN, RUMBLE, SEJUANI, TEAM, VI,
    data, live, rs, state, table,
)

W = EngineConfig().comp_weight


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "test.sqlite")


def engine(profiles=PROFILES, **cfg):
    return DraftEngine(replace(data(), profiles=profiles), TEAM, EngineConfig(**cfg))


def fit_of(sugs, champ):
    return next(s for s in sugs if s.champ_id == champ).comp_fit


# ---------- the coherence term ----------
def test_zero_at_our_first_pick_even_with_a_team_style():
    for target in (None, "engage"):
        sugs = engine(comp_target=target).suggest_picks(state(*BANS), k=50)
        assert sugs and all(s.comp_fit == 0 for s in sugs)


def test_damped_in_picks_one_to_three_full_at_four_and_five():
    # Team style Engage; Rakan's engage share is 0.7: fit = w * damping * (0.7 - 1/5), five identities
    eng = engine(comp_target="engage")
    pick2 = state(*BANS, SEJUANI, VI, GNAR)  # ours: Sejuani -> B2 (1 locked)
    pick3 = state(*BANS, SEJUANI, VI, GNAR, KAISA)  # B3 (2 locked)
    pick4 = state(*BANS, KSANTE, GNAR, VI, KAISA, SEJUANI, ORIANNA, *(SKIPPED,) * 4, AZIR)  # B4 (3 locked)
    base = W * (0.7 - 1 / 5)
    damping = EngineConfig().comp_damping
    assert damping[0] == 0 and damping[1] < damping[2] < damping[3] == damping[4] == 1
    for st, n in ((pick2, 1), (pick3, 2), (pick4, 3)):
        assert len(st.our_picks) == n and st.is_our_turn
        assert fit_of(eng.suggest_picks(st, k=50), RAKAN) == pytest.approx(base * damping[n])


def test_value_includes_the_term_but_win_prob_does_not():
    st = state(*BANS, KSANTE, GNAR, VI, KAISA, SEJUANI, ORIANNA, *(SKIPPED,) * 4, AZIR)
    on = {s.champ_id: s for s in engine(comp_target="engage").suggest_picks(st, k=50)}
    off = {s.champ_id: s for s in engine(comp_coherence=False).suggest_picks(st, k=50)}
    for c, s in on.items():
        assert s.win_prob == pytest.approx(off[c].win_prob)
        assert s.value == pytest.approx(off[c].value + _LO_TO_PROB * s.comp_fit)
        assert off[c].comp_fit == 0


# Rumble played as a pure dive champion in this test: a pick that completes Engage
DIVE_RUMBLE = replace(PROFILES, styles={**PROFILES.styles, RUMBLE: style(0.0, 1.0, 0.0)})


def test_completing_the_target_style_ranks_higher_at_pick_four():
    # ours: K'Sante, Kai'Sa, Nautilus (Dive, converging); R4 Vi; B4/B5 ours: jungle + mid open.
    # Mid: Azir (main) beats Rumble (learning) on meta + comfort; Rumble completes the comp's Dive.
    st = state(*BANS, *PHASE1, *(SKIPPED,) * 4, VI)
    assert len(st.our_picks) == 3
    def rank(eng):
        return [s.champ_id for s in eng.suggest_picks(st, k=50, role="mid")].index(RUMBLE)
    off = rank(engine(DIVE_RUMBLE, comp_coherence=False))
    on = rank(engine(DIVE_RUMBLE, comp_weight=0.6))
    assert on < off
    target, source = engine(DIVE_RUMBLE).comp_target(st)
    assert (target, source) == ("engage", "comp")  # no team style: the style the comp converges on
    # the same pick at our pick 2 moves much less
    early = state(*BANS, KAISA, VI, GNAR)
    eng = engine(DIVE_RUMBLE, comp_weight=0.6)
    late_fit = fit_of(eng.suggest_picks(st, k=50, role="mid"), RUMBLE)
    early_fit = fit_of(eng.suggest_picks(early, k=50, role="mid"), RUMBLE)
    assert 0 < early_fit < late_fit / 2


def test_team_style_beats_the_converging_style():
    st = state(*BANS, *PHASE1, *(SKIPPED,) * 4, VI)  # comp converges on Dive
    assert engine(comp_target="poke").comp_target(st) == ("poke", "team")
    assert engine(comp_target="mixed").comp_target(st) == (None, "team")  # "Mixed" set by hand: no pull
    assert engine(comp_coherence=False).comp_target(st) == (None, None)
    sugs = engine(comp_target="mixed").suggest_picks(st, k=50)
    assert all(s.comp_fit == 0 for s in sugs)


def test_coherence_is_the_share_of_the_target_minus_an_even_spread():
    # merged identities: no front-liner bonus any more; share - 1/5
    eng = engine(comp_target="protect")
    ours = [(AZIR, "mid"), (KAISA, "adc")]
    damp = eng.cfg.comp_damping[2]
    assert eng.coherence(ours, ORIANNA, "mid", "protect") == pytest.approx(W * damp * (0.7 - 1 / 5))
    assert eng.coherence(ours, KSANTE, "top", "protect") == pytest.approx(W * damp * (0.6 - 1 / 5))
    assert eng.coherence(ours, VI, "jungle", "protect") == pytest.approx(W * damp * (0.0 - 1 / 5))


def test_live_payload_shows_the_target_and_reason():
    st = state(*BANS, *PHASE1, *(SKIPPED,) * 4, VI)
    view = build_live_view(engine(DIVE_RUMBLE, comp_weight=0.6), CATALOG, live(st), "fixture", None)
    assert view["comp"]["ours"]["target"] == {"style": "engage", "label": "Engage", "source": "comp"}
    assert view["comp"]["theirs"].get("target") is None
    view = build_live_view(engine(comp_target="poke"), CATALOG, live(st), "fixture", None)
    assert view["comp"]["ours"]["target"]["source"] == "team"


def test_settings_round_trip_and_hand_style_reaches_the_engine(db):
    assert settings_payload(db)["comp_coherence"] is True
    assert settings_payload(db)["comp_weight"] == pytest.approx(W)
    save_engine_settings(db, comp_coherence=False, comp_weight=0.3)
    cfg = engine_config(db)
    assert cfg.comp_coherence is False and cfg.comp_weight == pytest.approx(0.3)
    with pytest.raises(ValueError):
        save_engine_settings(db, comp_weight=2.0)
    set_hand_style(db, "engage")
    assert engine_config(db).comp_target == "engage"


# ---------- Team page: the same labels ----------
def test_team_page_uses_the_comp_style_labels(db):
    assert list(TEAM_STYLES) == [*STYLES, "mixed"]
    assert all(TEAM_STYLES[k] == STYLE_LABELS[k] for k in STYLES)
    picks = [(VI, "jungle"), (RAKAN, "support"), (KAISA, "adc"), (KSANTE, "top"), (AZIR, "mid")]
    assert classify(PROFILES, picks) == "engage"
    assert classify(PROFILES, [(AZIR, "mid"), (ORIANNA, "mid"), (GNAR, "top")]) == "protect"
    # hand styles saved under the old vocabulary read as identities
    for old, new in (("front_to_back", "protect"), ("dive", "engage"), ("poke", "poke"), ("engage", "protect"),
                     ("balanced", "mixed")):
        with Session(db) as s:
            row = s.get(AppSetting, "team_style") or AppSetting(key="team_style", value="")
            row.value = '{"style": "%s"}' % old
            s.add(row)
            s.commit()
        assert get_hand_style(db) == new, old
    set_hand_style(db, "engage")  # a new save is read as written
    assert get_hand_style(db) == "engage"
    set_hand_style(db, "mixed")
    assert get_hand_style(db) == "mixed"
    with pytest.raises(ValueError):
        set_hand_style(db, "front_to_back")


# ---------- varied "Next 2 picks" ----------
PAIR_TEAM = OurTeam.from_tiers({
    "top": {KSANTE: "main", RUMBLE: "main"}, "jungle": {SEJUANI: "main"},
    "mid": {AZIR: "main", ORIANNA: "main"}, "adc": {KAISA: "main"}, "support": {NAUTILUS: "main", RAKAN: "main"},
}, {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")})
PAIR_STATE = state(*BANS, SEJUANI, VI, GNAR)  # B2/B3 ours: top, mid, adc, support open


def pair_engine(kaisa_wr, **cfg):
    # everyone else at 51% in their role: the next-best fresh pairs are within ~1pp of each other
    meta = {**META, KAISA: {"adc": rs(kaisa_wr, 0.12)}, KSANTE: {"top": rs(0.51, 0.10)},
            RUMBLE: {"top": rs(0.51, 0.04), "mid": rs(0.49, 0.02)}}
    eng_data = EngineData({"gold": BracketStats(meta)}, NAMES, synergy=table({}), damage={})
    return DraftEngine(eng_data, PAIR_TEAM, EngineConfig(**cfg))


def shown(pairs):
    return [(p.first.champ_id, p.second.champ_id) for p in pairs]


def test_pairs_vary_when_one_champion_is_only_slightly_better():
    pairs = pair_engine(0.52).suggest_pick_pairs(PAIR_STATE, k=3)
    assert len(pairs) == 3
    champs = [c for pair in shown(pairs) for c in pair]
    assert KAISA in shown(pairs)[0]  # the best pair still leads...
    assert champs.count(KAISA) < 3  # ...but ~1pp doesn't put Kai'Sa in every pair
    assert len(set(champs)) >= 5
    assert (RUMBLE, ORIANNA) in shown(pairs)  # fresh champions beat repeating Kai'Sa for 1pp
    assert [p.win_prob for p in pairs] == sorted((p.win_prob for p in pairs), reverse=True)  # shown best first
    old = pair_engine(0.52, pair_repeat_margin=0.0).suggest_pick_pairs(PAIR_STATE, k=3)
    assert all(KAISA in pair for pair in shown(old))  # pure ranking: Kai'Sa in all three


def test_pairs_repeat_a_champion_whose_call_is_that_strong():
    pairs = pair_engine(0.62).suggest_pick_pairs(PAIR_STATE, k=3)
    assert all(KAISA in pair for pair in shown(pairs))
    assert [p.win_prob for p in pairs] == sorted((p.win_prob for p in pairs), reverse=True)
