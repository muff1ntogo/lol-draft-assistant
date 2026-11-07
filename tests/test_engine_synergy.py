"""Synergy visualiser, Stage C: role-aware synergy in the draft engine, damage balance, pick-ahead pairs,
synergy reasons, optional ban Comp synergy. See docs/synergy-visualizer.md."""
import random
import time

import pytest

from draftassist.api.live import build_live_view
from draftassist.data.provider import ChampionCatalog
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import (
    BracketStats,
    EngineConfig,
    EngineData,
    OurTeam,
    PoolChamp,
    RoleStat,
)
from draftassist.engine.simulate import synthetic_data
from draftassist.engine.state import SKIPPED, DraftState
from draftassist.engine.synergy import PairScore, SynergyTable
from draftassist.lcu.parser import LiveDraft, Slot

KSANTE, GNAR, RUMBLE, SEJUANI, VI, AZIR, ORIANNA, KAISA, VARUS, NAUTILUS, RAKAN, RELL = range(1, 13)
NAMES = {KSANTE: "K'Sante", GNAR: "Gnar", RUMBLE: "Rumble", SEJUANI: "Sejuani", VI: "Vi", AZIR: "Azir",
         ORIANNA: "Orianna", KAISA: "Kai'Sa", VARUS: "Varus", NAUTILUS: "Nautilus", RAKAN: "Rakan", RELL: "Rell"}
BANS = (SKIPPED,) * 6
PP = 0.04


def rs(wr, pr=0.05):
    return RoleStat(wr, pr)


META = {
    KSANTE: {"top": rs(0.50, 0.10)}, GNAR: {"top": rs(0.51, 0.08)},
    RUMBLE: {"top": rs(0.50, 0.04), "mid": rs(0.49, 0.02)},
    SEJUANI: {"jungle": rs(0.52, 0.09)}, VI: {"jungle": rs(0.51, 0.06)},
    AZIR: {"mid": rs(0.48, 0.05)}, ORIANNA: {"mid": rs(0.51, 0.08)},
    KAISA: {"adc": rs(0.50, 0.12)}, VARUS: {"adc": rs(0.51, 0.07)},
    NAUTILUS: {"support": rs(0.51, 0.10)}, RAKAN: {"support": rs(0.51, 0.05)}, RELL: {"support": rs(0.51, 0.04)},
}


def s(x):
    return PairScore(x, x, 500, False)


def table(pairs):
    return SynergyTable("16.21", "all", {(a, b) if a < b else (b, a): s(v) for (a, b), v in pairs.items()}, {})


SYN = table({
    (("adc", KAISA), ("support", NAUTILUS)): 0.10,
    (("jungle", SEJUANI), ("support", NAUTILUS)): 0.05,
    (("mid", KAISA), ("support", NAUTILUS)): 0.30,  # wrong role for Kai'Sa: must not count
    (("jungle", VI), ("top", GNAR)): 0.08,  # an enemy pair
})
PHYS, MAGIC = (0.9, 0.08, 0.02), (0.1, 0.85, 0.05)
DAMAGE = {(KSANTE, "top"): PHYS, (SEJUANI, "jungle"): PHYS, (KAISA, "adc"): PHYS, (NAUTILUS, "support"): PHYS,
          (AZIR, "mid"): MAGIC, (ORIANNA, "mid"): MAGIC}
TEAM = OurTeam.from_tiers({
    "top": {KSANTE: "main", RUMBLE: "comfortable"}, "jungle": {SEJUANI: "main"},
    "mid": {AZIR: "main", ORIANNA: "comfortable", RUMBLE: "learning"},
    "adc": {KAISA: "comfortable"}, "support": {NAUTILUS: "main", RAKAN: "comfortable"},
}, {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")})


def data(syn=SYN, damage=DAMAGE):
    return EngineData({"gold": BracketStats(META)}, NAMES, synergy=syn, damage=damage)


def state(*actions):
    return DraftState(TOURNAMENT, "blue", tuple(actions))


# ---------- 1. the synergy term ----------
def test_synergy_term_is_the_sum_of_role_aware_pair_scores():
    # blue picks B1, B2, B3 = Kai'Sa, Nautilus, Sejuani; red picks R1, R2 = Vi, Gnar
    st = state(*BANS, KAISA, VI, GNAR, NAUTILUS, SEJUANI)
    ev = DraftEngine(data(), TEAM).evaluate(st)
    assert ev.our_roles == {KAISA: "adc", NAUTILUS: "support", SEJUANI: "jungle"}
    t = SYN
    expected = sum(t.pair(ev.our_roles[a], a, ev.our_roles[b], b).s
                   for a, b in ((KAISA, NAUTILUS), (KAISA, SEJUANI), (NAUTILUS, SEJUANI)))
    assert ev.terms.syn_us == pytest.approx(expected) == pytest.approx(0.15)  # not the 0.30 mid Kai'Sa pair
    assert ev.terms.syn_them == pytest.approx(0.08)
    no_syn = DraftEngine(data(syn=table({})), TEAM).evaluate(st)
    assert ev.terms.total - no_syn.terms.total == pytest.approx(0.15 - 0.08)


# ---------- 2. damage balance ----------
def test_damage_penalty_above_85_percent_only():
    eng = DraftEngine(data(), TEAM)
    all_ad = state(*BANS, KAISA, VI, GNAR, NAUTILUS, SEJUANI)  # 90% physical each
    assert eng.evaluate(all_ad).terms.damage == pytest.approx(-0.05)
    mixed = state(*BANS, KAISA, VI, GNAR, NAUTILUS, AZIR)  # (0.9 + 0.9 + 0.1) / 3 = 63% physical
    assert eng.evaluate(mixed).terms.damage == 0.0
    two = state(*BANS, KAISA, VI, GNAR, NAUTILUS)  # too early to judge a comp
    assert eng.evaluate(two).terms.damage == 0.0
    off = DraftEngine(data(), TEAM, EngineConfig(damage_balance=False))
    assert off.evaluate(all_ad).terms.damage == 0.0
    harsher = DraftEngine(data(), TEAM, EngineConfig(damage_penalty=-0.2))
    assert harsher.evaluate(all_ad).terms.damage == pytest.approx(-0.2)
    edge = {k: (0.85, 0.15, 0.0) for k in DAMAGE}  # exactly 85%: not above
    assert DraftEngine(data(damage=edge), TEAM).evaluate(all_ad).terms.damage == 0.0
    assert eng.evaluate(all_ad).terms.total == pytest.approx(
        DraftEngine(data(), TEAM, EngineConfig(damage_balance=False)).evaluate(all_ad).terms.total - 0.05)


# ---------- 3. pick-ahead ----------
def test_back_to_back_detection():
    eng = DraftEngine(data(), TEAM)
    assert not eng.back_to_back(state(*BANS))  # B1 alone
    assert eng.back_to_back(state(*BANS, SEJUANI, RAKAN, GNAR))  # B2/B3
    assert not eng.back_to_back(state(*BANS, SEJUANI, RAKAN, GNAR, KAISA))  # B3 is the second of the two
    red = DraftState(TOURNAMENT, "red", (*BANS, SEJUANI))
    assert eng.back_to_back(red)  # R1/R2


# Kai'Sa + Nautilus at +6.25pp: enough to beat two mains (K'Sante + Nautilus) without synergy
DUO = table({(("adc", KAISA), ("support", NAUTILUS)): 0.25})


def test_pick_pairs_from_open_roles_respect_guardrails():
    # bans: Orianna banned; B1 Sejuani (ours), R1 Rakan, R2 Gnar: B2/B3 are ours
    st = state(ORIANNA, *BANS[1:], SEJUANI, RAKAN, GNAR)
    eng = DraftEngine(data(syn=DUO), TEAM)
    pairs = eng.suggest_pick_pairs(st, k=3)
    assert len(pairs) == 3
    sets = [frozenset((p.first.champ_id, p.second.champ_id)) for p in pairs]
    assert len(set(sets)) == 3
    taken = st.unavailable()
    for p in pairs:
        a, b = p.first, p.second
        assert a.champ_id not in taken and b.champ_id not in taken and a.champ_id != b.champ_id
        assert a.role != b.role and {a.role, b.role} <= {"top", "mid", "adc", "support"}
        assert TEAM.entry(a.champ_id, a.role) and TEAM.entry(b.champ_id, b.role)  # from the open roles' pools
        assert 0 < p.win_prob < 1
        assert p.synergy_pp == pytest.approx(25 * DUO.pair(a.role, a.champ_id, b.role, b.champ_id).s)
    assert [p.win_prob for p in pairs] == sorted((p.win_prob for p in pairs), reverse=True)
    assert sets[0] == frozenset((KAISA, NAUTILUS)) and pairs[0].synergy_pp == pytest.approx(6.25)
    no_syn = DraftEngine(data(syn=table({})), TEAM).suggest_pick_pairs(st, k=3)
    assert frozenset((no_syn[0].first.champ_id, no_syn[0].second.champ_id)) == frozenset((KSANTE, NAUTILUS))
    assert eng.suggest_pick_pairs(state(*BANS), k=3) == []  # not back to back


# ---------- 4. reasons ----------
def test_synergy_reason_names_the_locked_partner():
    syn = table({(("adc", KAISA), ("support", NAUTILUS)): 3.4 * PP})
    eng = DraftEngine(data(syn=syn, damage={}), TEAM)
    st = state(*BANS, KAISA, VI, GNAR)
    nautilus = next(p for p in eng.suggest_picks(st, k=20, role="support") if p.champ_id == NAUTILUS)
    assert nautilus.reason == "Support main, +3.4pp with your locked Kai'Sa"


# ---------- 5. ban Comp synergy (flag) ----------
def test_ban_comp_counts_enemy_synergy_only_with_flag_in_phase_two():
    syn = table({(("jungle", VI), ("top", GNAR)): 0.2})
    # phase 1 picks done: B1 K'Sante, R1 Gnar, R2 Rell, B2 Kai'Sa, B3 Nautilus, R3 Varus; red bans next
    picks = (KSANTE, GNAR, RELL, KAISA, NAUTILUS, VARUS)
    phase2 = DraftState(TOURNAMENT, "blue", (*BANS, *picks, AZIR))  # red banned Azir; our ban 2
    assert phase2.step.kind == "ban" and phase2.step.phase == 2 and phase2.is_our_turn

    def vi_comp(cfg, st):
        eng = DraftEngine(data(syn=syn, damage={}), TEAM, cfg)
        return next(b for b in eng._ban_candidates(st) if b.champ_id == VI).comp

    assert vi_comp(EngineConfig(ban_comp_synergy=True), phase2) == pytest.approx(
        vi_comp(EngineConfig(), phase2) + 0.2 * 1.0)  # Vi can only go jungle
    phase1 = DraftState(TOURNAMENT, "blue", (SKIPPED,) * 2)  # still in ban phase 1: no enemy picks
    assert vi_comp(EngineConfig(ban_comp_synergy=True), phase1) == vi_comp(EngineConfig(), phase1)


# ---------- live screen + response time ----------
CATALOG = ChampionCatalog("16.21.1", {c: (n, n) for c, n in NAMES.items()})


def live(st, our_slots=()):
    return LiveDraft(st, tuple(our_slots), (), "BAN_PICK", 20.0, None, None, 1)


def test_live_view_shows_next_two_picks_when_back_to_back():
    eng = DraftEngine(data(syn=DUO), TEAM)
    st = state(ORIANNA, *BANS[1:], SEJUANI, RAKAN, GNAR)
    view = build_live_view(eng, CATALOG, live(st, [Slot(1, "jungle", SEJUANI, 0)]), "fixture", None)
    nxt = view["center"]["next_two"]
    assert len(nxt) == 3 and len(view["center"]["cards"]) == 5  # our pick 2: 5 cards (docs/comp-style.md)
    first = nxt[0]
    assert {c["champ"]["id"] for c in first["picks"]} == {KAISA, NAUTILUS}
    assert first["synergy_pp"] == pytest.approx(6.25, abs=0.05) and 0 < first["win_prob"] < 1
    assert {c["role_label"] for c in first["picks"]} == {"ADC", "Support"}
    naut = next(c for c in first["picks"] if c["champ"]["id"] == NAUTILUS)
    assert naut["meta_wr"] == 0.51 and naut["bracket"] == "gold"
    assert naut["player_games"] == 0 and naut["player_wr"] is None  # no recent games in this fixture

    played = OurTeam({**TEAM.pools, "support": {NAUTILUS: PoolChamp("main", 0.2, games=13, wins=8)}}, TEAM.brackets)
    rec = DraftEngine(data(syn=DUO), played).pick_record(NAUTILUS, "support")
    assert rec["player_games"] == 13 and rec["player_wr"] == pytest.approx(8 / 13)
    assert DraftEngine(data(), TEAM).pick_record(VI, "jungle")["player_games"] == 0  # off-pool
    single = build_live_view(eng, CATALOG, live(state(*BANS)), "fixture", None)
    assert single["center"]["mode"] == "our_pick" and single["center"].get("next_two") is None


def test_pick_turn_budget_with_pairs_on_full_size_data():
    full, team = synthetic_data()
    eng = DraftEngine(full, team)
    names = {c: (str(c), str(c)) for c in full.names}
    catalog = ChampionCatalog("16.21.1", names)
    rng = random.Random(5)
    worst = 0.0
    pool = sorted(team.pool_champs())
    others = [c for c in sorted(full.champions()) if c not in team.pool_champs()]
    for _ in range(10):
        bans = rng.sample(others, 6)
        b1 = rng.choice(pool)
        r1, r2 = rng.sample([c for c in others if c not in bans], 2)
        st = DraftState(TOURNAMENT, "blue", (*bans, b1, r1, r2))
        assert eng.back_to_back(st)
        t0 = time.perf_counter()
        view = build_live_view(eng, catalog, live(st), "fixture", None)
        worst = max(worst, time.perf_counter() - t0)
        assert view["center"]["next_two"]
    assert worst < 0.200


# ---------- settings ----------
def test_damage_and_ban_synergy_settings_round_trip(engine):
    from draftassist.data.settings_store import (
        engine_config,
        save_engine_settings,
        settings_payload,
    )

    cfg = engine_config(engine)
    assert (cfg.damage_balance, cfg.damage_penalty, cfg.ban_comp_synergy) == (True, -0.05, False)
    save_engine_settings(engine, damage_balance=False, damage_penalty=-0.1, ban_comp_synergy=True)
    save_engine_settings(engine, alpha=1.2)  # other values are kept
    cfg = engine_config(engine)
    assert (cfg.alpha, cfg.damage_balance, cfg.damage_penalty, cfg.ban_comp_synergy) == (1.2, False, -0.1, True)
    assert settings_payload(engine)["damage_penalty"] == -0.1
    with pytest.raises(ValueError):
        save_engine_settings(engine, damage_penalty=0.5)
