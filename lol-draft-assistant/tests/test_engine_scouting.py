"""docs/enemy-scouting.md, Stage B: scouting in the engine."""
import pytest

from draftassist.data.scout import PlayerPool, assign_roles, build_scouting
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import ROLES
from draftassist.engine.scouting import (
    CONFIDENCE_K,
    MASTERY_ONLY_GAMES,
    SCOUT_WEIGHT,
    EnemyScouting,
    ScoutedPlayer,
)
from draftassist.engine.state import DraftState

ROLE_SHARES = {1: {"top": 0.9}, 2: {"top": 0.8, "mid": 0.2}, 3: {"mid": 1.0}, 4: {"top": 0.05, "jungle": 0.95}}


def player(role="top", games=(), mastery=(), name="Foe"):
    any_role = {}
    for c, g, _ in games:
        any_role[c] = any_role.get(c, 0) + g
    return ScoutedPlayer(name, role, tuple(games), tuple(sorted(any_role.items())), tuple(mastery))


# ---------- the blend ----------
def test_the_blend_keeps_the_total_and_leans_on_the_player_by_games():
    scouting = EnemyScouting((player(games=((2, 10, 6),)),))
    meta = {1: 0.6, 2: 0.4}
    out = scouting.blend("top", meta, ROLE_SHARES)
    s = SCOUT_WEIGHT * 10 / (10 + CONFIDENCE_K)
    assert sum(out.values()) == pytest.approx(1.0)
    assert out[2] == pytest.approx((1 - s) * 0.4 + s * 1.0)
    assert out[1] == pytest.approx((1 - s) * 0.6)


def test_a_role_with_nobody_scouted_is_the_meta():
    scouting = EnemyScouting((player(role="mid", games=((3, 10, 5),)),))
    assert scouting.blend("top", {1: 0.6, 2: 0.4}, ROLE_SHARES) == {1: 0.6, 2: 0.4}


def test_with_no_games_mastery_spreads_over_the_roles_champions():
    p = player(mastery=((4, 900_000), (1, 300_000), (3, 100_000)))
    share, n = p.share(ROLE_SHARES)
    assert share == {1: 1.0} and n == MASTERY_ONLY_GAMES  # 4 is a jungler, 3 a mid laner: not top


def test_champions_that_cant_be_picked_drop_out_of_their_share():
    scouting = EnemyScouting((player(games=((1, 5, 3), (2, 5, 2))),))
    out = scouting.blend("top", {1: 0.5, 2: 0.5}, ROLE_SHARES, allowed=[2])
    s = SCOUT_WEIGHT * 10 / (10 + CONFIDENCE_K)
    assert out[2] == pytest.approx((1 - s) * 0.5 + s * 1.0)


def test_pool_membership_and_owner():
    a = player(role="top", games=((1, 3, 2), (2, 1, 1)), mastery=((9, 10), (8, 5), (7, 4), (6, 1)))
    b = player(role="mid", games=((1, 5, 1),), name="Other")
    scouting = EnemyScouting((a, b))
    assert a.plays(1) and not a.plays(2) and a.plays(7) and not a.plays(6)
    assert scouting.owner(1).name == "Other"  # more games on it
    assert scouting.owner(1, open_roles=["top"]).name == "Foe"
    assert scouting.owner(5) is None


# ---------- from stored pools ----------
def pool(rid, games, mastery=()):
    return PlayerPool(rid, "done", {r: dict(c) for r, c in games.items()}, tuple(mastery))


def test_roles_come_from_the_client_else_the_most_played_free_role():
    pools = {"A#1": pool("A#1", {"top": {1: (10, 5)}}), "B#1": pool("B#1", {"top": {2: (4, 2)}, "mid": {3: (3, 1)}}),
             "C#1": pool("C#1", {})}
    assert assign_roles(pools, {"C#1": "jungle"}) == {"C#1": "jungle", "A#1": "top", "B#1": "mid"}
    assert assign_roles(pools, {"B#1": "top"}) == {"B#1": "top"}  # A only plays top, which is taken


def test_build_scouting():
    pools = {"Foe Name#EUW": pool("Foe Name#EUW", {"top": {1: (6, 4)}, "mid": {3: (2, 1)}}, [(1, 100)])}
    scouting = build_scouting(pools, {"Foe Name#EUW": None})
    p = scouting.player("top")
    assert p.name == "Foe Name" and p.games == ((1, 6, 4),) and dict(p.any_role) == {1: 6, 3: 2}
    assert build_scouting({}, {}) is None


def test_the_draft_keeps_its_scouting():
    scouting = EnemyScouting((player(),))
    state = DraftState(TOURNAMENT, "blue", scouting=scouting)
    assert state.apply(1).scouting is scouting and state.with_provisional([5]).scouting is scouting


# ---------- in the engine ----------
@pytest.fixture
def eng(make_services):
    return make_services().engine


def rare_top(eng):
    """A champion their bracket rarely plays top: scouting should make it matter."""
    stats = eng.enemy_stats
    rates = [(s.pick_rate, c) for c in eng.data.champions() if (s := stats.role_stat(c, "top")) and s.pick_rate > 0]
    return min(rates)[1]


def test_their_toplaners_main_becomes_likely_and_a_ban(eng):
    champ = rare_top(eng)
    plain = DraftState(TOURNAMENT, "blue")
    scouted = DraftState(TOURNAMENT, "blue", scouting=EnemyScouting((player(games=((champ, 20, 14),), name="Main"),)))
    assert eng.enemy_pick_probs(scouted)[champ] > 5 * eng.enemy_pick_probs(plain).get(champ, 0.0)
    before = {s.champ_id: s for s in eng.suggest_bans(plain, k=200)}
    after = {s.champ_id: s for s in eng.suggest_bans(scouted, k=200)}
    assert after[champ].value > before.get(champ, after[champ]).value or champ not in before
    assert after[champ].threat > (before[champ].threat if champ in before else 0.0)
    assert after[champ].reason.startswith("Their top laner (Main): 20 games, 70% · ")
    assert after[champ].scout_role == "top"


def test_a_filled_role_no_longer_names_the_player(eng):
    champ = rare_top(eng)
    scouting = EnemyScouting((player(games=((champ, 20, 14),)),))
    state = DraftState(TOURNAMENT, "blue", scouting=scouting)
    assert state.scouting.owner(champ, open_roles=[r for r in ROLES if r != "top"]) is None


def test_bans_split_into_their_pools_and_the_best_of_the_rest(eng):
    # their top plays six champions that are already worth banning (a ban must be worth something: Rule 3)
    worth = [s.champ_id for s in eng.suggest_bans(DraftState(TOURNAMENT, "blue"), k=200)]
    tops = [c for c in worth if (s := eng.enemy_stats.role_stat(c, "top")) and s.pick_rate > 0][:6]
    scouting = EnemyScouting((player(games=tuple((c, 4, 2) for c in tops)),))
    state = DraftState(TOURNAMENT, "blue", scouting=scouting)
    pool, off = eng.suggest_bans_scouted(state, 5, 2)
    assert len(pool) == 5 and len(off) == 2
    assert all(s.champ_id in tops and s.scout_role == "top" for s in pool)
    assert all(s.champ_id not in tops and s.scout_role is None for s in off)
    assert [s.value for s in pool] == sorted((s.value for s in pool), reverse=True)


def test_short_on_pool_bans_the_rest_fill_the_gap(eng):
    champ = rare_top(eng)
    state = DraftState(TOURNAMENT, "blue", scouting=EnemyScouting((player(games=((champ, 20, 14),)),)))
    pool, off = eng.suggest_bans_scouted(state, 3, 1)
    assert [s.champ_id for s in pool] == [champ] and len(off) == 3


def test_an_off_meta_pick_in_their_pool_is_scored_not_a_crash(eng):
    stats = eng.enemy_stats
    off_meta = next(c for c in sorted(eng.data.champions()) if stats.role_stat(c, "jungle") is None)
    state = DraftState(TOURNAMENT, "blue", scouting=EnemyScouting((player(role="jungle", games=((off_meta, 15, 9),)),)))
    pool, _ = eng.suggest_bans_scouted(state, 5, 2)
    assert eng.enemy_pick_probs(state)[off_meta] > 0
    assert all(s.scout_role == "jungle" for s in pool)


def test_without_scouting_bans_are_unchanged(eng):
    state = DraftState(TOURNAMENT, "blue")
    assert [s.champ_id for s in eng.suggest_bans(state, k=5)] == [s.champ_id for s in eng.suggest_bans(state, k=5)]
    assert all(s.scout_role is None for s in eng.suggest_bans(state, k=5))
