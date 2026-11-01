import random

import pytest

from draftassist.engine.lane_ban import (
    NO_ELIGIBLE_POOL,
    NO_MATCHUP_DATA,
    LaneBanConfig,
    MappingLaneData,
    ban_scores,
    expected_quality,
    role_ban_table,
    team_lane_ban,
)

A, B, C = 1, 2, 3


# 1. Worked example ---------------------------------------------------------------
def test_worked_example():
    w = {A: 0.3, B: 0.2, C: 0.5}
    resp = {A: -2.0, B: 3.0, C: 0.0}
    assert expected_quality(w, resp) == pytest.approx(0.0)
    scores = ban_scores(w, resp)
    assert round(scores[A].ban, 3) == 0.857
    assert round(scores[B].ban, 3) == -0.750
    assert round(scores[C].ban, 3) == 0.000
    without_a = expected_quality({B: 0.2, C: 0.5}, resp)
    assert scores[A].ban == pytest.approx(without_a - expected_quality(w, resp))


# Fixture provider for the role-level tests -------------------------------------
OPP1, OPP2, OPP3 = 101, 102, 103
OURS_MAIN, OURS_COMF, OURS_LEARN = 201, 202, 203


def provider(**overrides):
    base = dict(
        pick_rates={"top": {OPP1: 0.12, OPP2: 0.08, OPP3: 0.05}},
        ban_rates={},
        matchups={
            ("top", OURS_MAIN, OPP1): 0.47,
            ("top", OURS_COMF, OPP1): 0.45,
            ("top", OURS_MAIN, OPP2): 0.52,
            ("top", OURS_COMF, OPP2): 0.55,
            ("top", OURS_MAIN, OPP3): 0.50,
        },
        role_wrs={("top", OURS_MAIN): 0.51, ("top", OURS_COMF): 0.49, ("top", OURS_LEARN): 0.50},
    )
    base.update(overrides)
    return MappingLaneData(**base)


POOL = {OURS_MAIN: "main", OURS_COMF: "comfortable"}


def rows_by_opp(table):
    return {r.opp: r for r in table.rows}


def test_resp_is_best_pool_answer_and_rows_sorted():
    table = role_ban_table(provider(), "top", POOL)
    rows = rows_by_opp(table)
    assert rows[OPP1].resp == pytest.approx(-3.0) and rows[OPP1].via == OURS_MAIN
    assert rows[OPP2].resp == pytest.approx(5.0) and rows[OPP2].via == OURS_COMF
    assert [r.ban for r in table.rows] == sorted((r.ban for r in table.rows), reverse=True)
    assert table.rows[0].opp == OPP1  # common and handled worst


def test_learning_tier_adjustment():
    data = provider(matchups={("top", OURS_LEARN, OPP1): 0.53})
    row = rows_by_opp(role_ban_table(data, "top", {OURS_LEARN: "learning"}))[OPP1]
    assert row.resp == pytest.approx(3.0 - 2.0)


# 2. Banned / picked champions excluded from opponents and pool -------------------
def test_unavailable_excluded_from_opponents_and_pool():
    table = role_ban_table(provider(), "top", POOL, unavailable={OPP2, OURS_MAIN})
    rows = rows_by_opp(table)
    assert set(rows) == {OPP1, OPP3}
    assert all(r.via != OURS_MAIN for r in table.rows)
    assert rows[OPP1].via == OURS_COMF and rows[OPP1].resp == pytest.approx(-5.0)


# 3. Only opponent ----------------------------------------------------------------
def test_single_opponent_is_flagged_not_an_error():
    data = provider(pick_rates={"top": {OPP1: 0.12}})
    (row,) = role_ban_table(data, "top", POOL).rows
    assert row.ban == 0.0 and row.degenerate
    assert ban_scores({A: 0.3, B: 1e-9}, {A: 1.0, B: -5.0})[A].degenerate


# 4. Locked pick ------------------------------------------------------------------
def test_locked_pick_is_the_only_answer():
    table = role_ban_table(provider(), "top", POOL, unavailable={OURS_MAIN}, locked=OURS_MAIN)
    rows = rows_by_opp(table)
    assert all(r.via in (OURS_MAIN, None) for r in table.rows)
    assert rows[OPP2].resp == pytest.approx(2.0)  # Comfortable answer (+5) no longer counts


# 5. Enemy filled the role --------------------------------------------------------
def test_enemy_filled_role_contributes_nothing():
    table = role_ban_table(provider(), "top", POOL, enemy_filled=True)
    assert table.enemy_filled and table.rows
    assert all(r.ban == 0.0 for r in table.rows)


# 6. Flex champion sums across roles ----------------------------------------------
def test_team_lane_ban_sums_flex_champion():
    data = provider(
        pick_rates={"top": {OPP1: 0.12, OPP2: 0.08}, "mid": {OPP1: 0.06, OPP3: 0.10}},
        matchups={("top", OURS_MAIN, OPP1): 0.46, ("top", OURS_MAIN, OPP2): 0.51,
                  ("mid", OURS_COMF, OPP1): 0.44, ("mid", OURS_COMF, OPP3): 0.50},
    )
    tables = {
        "top": role_ban_table(data, "top", {OURS_MAIN: "main"}),
        "mid": role_ban_table(data, "mid", {OURS_COMF: "comfortable"}),
    }
    team = team_lane_ban(tables.values())
    top, mid = rows_by_opp(tables["top"]), rows_by_opp(tables["mid"])
    assert top[OPP1].ban > 0 and mid[OPP1].ban > 0
    assert team[OPP1] == pytest.approx(top[OPP1].ban + mid[OPP1].ban)
    assert team[OPP3] == pytest.approx(mid[OPP3].ban)


# 7. Options move results in the expected direction -------------------------------
def test_adjust_for_bans_raises_weight_of_often_banned_opponents():
    data = provider(ban_rates={OPP1: 0.40})
    on = rows_by_opp(role_ban_table(data, "top", POOL, config=LaneBanConfig(adjust_for_bans=True)))
    off = rows_by_opp(role_ban_table(data, "top", POOL, config=LaneBanConfig(adjust_for_bans=False)))
    assert on[OPP1].w == pytest.approx(0.12 / 0.6) and off[OPP1].w == pytest.approx(0.12)
    assert on[OPP1].pick_rate == off[OPP1].pick_rate == pytest.approx(0.12)
    assert on[OPP1].ban > off[OPP1].ban  # a bad matchup that's played more when available


def test_champion_avg_baseline_judges_strong_champions_harder():
    fifty = rows_by_opp(role_ban_table(provider(), "top", {OURS_MAIN: "main"}))
    avg = rows_by_opp(role_ban_table(provider(), "top", {OURS_MAIN: "main"},
                                     config=LaneBanConfig(delta_baseline="champion_avg")))
    # OURS_MAIN averages 51%, so every matchup delta drops by 1 pp.
    for opp in (OPP1, OPP2, OPP3):
        assert avg[opp].resp == pytest.approx(fifty[opp].resp - 1.0)


def test_no_matchup_data_falls_back_to_pool_baseline():
    data = provider(matchups={})
    rows = rows_by_opp(role_ban_table(data, "top", POOL))
    assert all(r.via is None and r.via_note == NO_MATCHUP_DATA for r in rows.values())
    assert rows[OPP1].resp == pytest.approx(1.0)  # best pool champion averages 51%
    assert all(r.ban == pytest.approx(0.0) for r in rows.values())  # equal Resp -> no preference


def test_no_eligible_pool_champion():
    rows = role_ban_table(provider(), "top", POOL, unavailable={OURS_MAIN, OURS_COMF}).rows
    assert rows and all(r.via_note == NO_ELIGIBLE_POOL and r.ban == pytest.approx(0.0) for r in rows)


# 8. Property: BanScore(X) == mu' - mu --------------------------------------------
@pytest.mark.parametrize("seed", range(500))
def test_ban_score_is_exact_change_in_expected_quality(seed):
    rng = random.Random(seed)
    n = rng.randint(2, 12)
    w = {i: rng.uniform(0.001, 0.3) for i in range(n)}
    resp = {i: rng.uniform(-15, 15) for i in range(n)}
    mu = expected_quality(w, resp)
    scores = ban_scores(w, resp)
    for x in w:
        rest = {k: v for k, v in w.items() if k != x}
        assert abs(scores[x].ban - (expected_quality(rest, resp) - mu)) < 1e-9


def test_missing_data_never_favours_banning_our_own_pool_champion():
    # OPP1 is also in our pool. With no matchup data, its Resp must equal every other opponent's
    # (the same baseline), so no ban preference comes from missing data alone.
    data = provider(matchups={}, role_wrs={("top", OPP1): 0.53, ("top", OURS_MAIN): 0.51})
    rows = rows_by_opp(role_ban_table(data, "top", {OPP1: "main", OURS_MAIN: "main"}))
    assert all(r.resp == pytest.approx(3.0) for r in rows.values())
    assert all(r.ban == pytest.approx(0.0) for r in rows.values())
