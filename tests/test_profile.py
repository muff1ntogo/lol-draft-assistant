"""docs/roster-profiles.md, Stage A: the profile columns, their filling and backfill, and the profile figures.
Acceptance: unit tests on planted games; a backfilled database gives the same figures as freshly fetched games."""
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from sqlmodel import Session, select

from draftassist.data.pools import set_players
from draftassist.data.riot import RateLimiter, RiotClient
from draftassist.db.models import (
    Draft,
    GameExecutionRole,
    Player,
    PlayerGame,
    PoolEntry,
)
from draftassist.db.session import init_db
from draftassist.learning.history import (
    backfill_game_stats,
    fetch_history,
    game_stats,
    mastery_rows,
    refresh_profiles,
)
from draftassist.learning.profile import (
    ExecutionRow,
    Game,
    PoolRow,
    also_plays,
    champion_rows,
    execution_figures,
    figures,
    in_window,
    load_inputs,
    mastery_top,
    pending_changes,
    pool_size_weighted,
    pool_strip,
    profile,
    rank_summary,
    roles_breakdown,
    summary,
)

NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000


def game(i, champ=897, role="top", win=True, days_ago=1, k=4, d=2, a=6, cs=210, vision=20, duration=1800, stats=True):
    extra = {"kills": k, "deaths": d, "assists": a, "cs": cs, "vision": vision, "duration_s": duration} if stats else {}
    return Game(f"EUW1_{i}", champ, role, win, NOW_MS - days_ago * DAY_MS - i, **extra)


# ---------- figures from planted games ----------
def test_figures_count_only_games_that_carry_stats():
    games = [game(1, k=10, d=2, a=4, cs=240, vision=30, duration=1800), game(2, win=False, k=2, d=6, a=2, cs=180, vision=10, duration=1200),
             game(3, stats=False)]
    f = figures(games)
    assert (f["games"], f["wins"], f["stat_games"]) == (3, 2, 2) and f["win_rate"] == pytest.approx(2 / 3)
    assert f["kda"] == pytest.approx((10 + 4 + 2 + 2) / 8)  # summed over the games, not a mean of ratios
    assert f["cs_min"] == pytest.approx(420 / 50) and f["vision_min"] == pytest.approx(40 / 50)
    assert f["kills"] == pytest.approx(6) and f["deaths"] == pytest.approx(4)

    empty = figures([])
    assert empty["games"] == 0 and empty["win_rate"] is None and empty["kda"] is None and empty["cs_min"] is None
    assert figures([game(1, stats=False)])["kda"] is None  # a game without stats gives no KDA, not 0
    assert figures([game(1, k=3, d=0, a=5)])["kda"] == pytest.approx(8)  # no deaths: kills + assists


def test_windows_last_20_games_or_60_days():
    games = [game(i, days_ago=i) for i in range(1, 31)] + [game(99, days_ago=70)]
    assert len(in_window(games, "60d", NOW_MS)) == 30  # the 70-day-old game is out
    last = in_window(games, "20", NOW_MS)
    assert len(last) == 20 and last[0].match_id == "EUW1_1" and last[-1].match_id == "EUW1_20"  # newest first


def test_roles_and_also_plays():
    games = [game(i) for i in range(8)] + [game(10 + i, role="mid") for i in range(2)] + [game(20, role=None)]
    roles = roles_breakdown(games)
    assert [(r["role"], r["games"]) for r in roles] == [("top", 8), ("mid", 2)]
    assert roles[0]["share"] == pytest.approx(0.8)  # of the games that have a role
    assert [r["role"] for r in also_plays(games, "top")] == ["mid"]  # 20% of their games
    assert also_plays([game(i) for i in range(9)] + [game(50, role="mid")], "top") == []  # 10%: not a flex player


# ---------- lane execution ----------
def ex(i, share, label="ahead"):
    return ExecutionRow(NOW_MS - (100 - i) * DAY_MS, "top", share, label)


def test_execution_needs_five_team_games():
    out = execution_figures([ex(i, 0.05) for i in range(4)])
    assert (out["games"], out["enough"], out["mean_pp"], out["label"], out["last"]) == (4, False, None, None, [])


def test_execution_mean_label_and_trend():
    winning = execution_figures([ex(i, s) for i, s in enumerate([0.04, 0.05, 0.06, 0.05, 0.04, 0.06])])
    assert winning["mean_pp"] == pytest.approx(5.0) and winning["label"] == "Wins lane" and winning["trend"] is None  # under 10 games
    assert winning["last"] == pytest.approx([4, 5, 6, 5, 4, 6])  # oldest first, in pp

    noisy = execution_figures([ex(i, s) for i, s in enumerate([0.08, -0.07, 0.06, -0.08, 0.02])])
    assert noisy["label"] == "Even"  # the band reaches 0
    assert execution_figures([ex(i, -0.05 - i / 1000) for i in range(6)])["label"] == "Loses lane"

    rising = execution_figures([ex(i, s, "ahead" if s > 0 else "behind") for i, s in enumerate([-0.04] * 5 + [0.04] * 5)])
    assert rising["trend"] == "up" and rising["labels"] == {"behind": 5, "ahead": 5}
    assert execution_figures([ex(i, s) for i, s in enumerate([0.04] * 5 + [-0.04] * 5)])["trend"] == "down"
    assert execution_figures([ex(i, 0.03) for i in range(12)])["trend"] == "flat"
    assert len(execution_figures([ex(i, 0.01) for i in range(30)])["last"]) == 20


# ---------- rank, mastery, pool ----------
def test_rank_summary_picks_the_higher_queue():
    entries = [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 40, "wins": 30, "losses": 25},
               {"queueType": "RANKED_FLEX_SR", "tier": "PLATINUM", "rank": "IV", "leaguePoints": 5, "wins": 12, "losses": 9},
               {"queueType": "RANKED_TFT", "tier": "DIAMOND", "rank": "I"}]
    r = rank_summary(entries)
    assert r["solo"]["label"] == "Gold II" and r["solo"]["lp"] == 40 and r["flex"]["label"] == "Platinum IV"
    assert r["best"]["queue"] == "flex"  # other games' ladders are ignored
    same_tier = rank_summary([{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "I", "leaguePoints": 0},
                              {"queueType": "RANKED_FLEX_SR", "tier": "GOLD", "rank": "II", "leaguePoints": 99}])
    assert same_tier["best"]["queue"] == "solo"  # division before LP
    assert rank_summary([{"queueType": "RANKED_SOLO_5x5", "tier": "MASTER", "rank": "I", "leaguePoints": 120}])["best"]["label"] == "Master"
    assert rank_summary(None) == {"solo": None, "flex": None, "best": None}


POOL = [PoolRow(897, "top", "main"), PoolRow(150, "top", "comfortable", scored_tier="main"), PoolRow(266, "top", "learning", scored_tier="comfortable", locked=True),
        PoolRow(122, "top", "comfortable", scored_tier="comfortable"), PoolRow(103, "mid", "learning", scored_tier="main")]


def test_pool_strip_size_and_pending():
    strip = pool_strip(POOL, "top", n=3)
    assert [c["champ_id"] for c in strip["champs"]] == [897, 122, 150] and strip["more"] == 1 and strip["size"] == 4  # tier order
    assert pool_size_weighted(POOL, "top") == pytest.approx(1 + 0.5 + 0.5 + 0.25)
    assert pending_changes(POOL, "top") == 1  # Gnar; the locked one and the unchanged one don't count
    assert pool_strip([], "top") == {"champs": [], "more": 0, "size": 0}


def test_champion_rows_and_what_is_not_in_the_pool():
    games = [game(i, champ=897) for i in range(6)] + [game(10 + i, champ=150, win=False) for i in range(2)] \
        + [game(20 + i, champ=39) for i in range(3)] + [game(30, champ=897, role="mid")]
    mastery = [[897, 250_000, NOW_MS], [39, 60_000, NOW_MS]]
    table = champion_rows(games, POOL, "top", mastery)
    assert [r["champ_id"] for r in table["pool"]] == [897, 150, 122, 266]  # tier, then games
    ksante = table["pool"][0]
    assert (ksante["games"], ksante["wins"], ksante["mastery"], ksante["in_pool"]) == (6, 6, 250_000, True)  # the mid game isn't counted
    assert table["pool"][1]["suggested_tier"] == "main" and table["pool"][3]["suggested_tier"] is None  # locked: no suggestion
    assert [(r["champ_id"], r["games"], r["in_pool"]) for r in table["not_in_pool"]] == [(39, 3, False)]
    top = mastery_top(mastery, [e.champ_id for e in POOL])
    assert [(m["champ_id"], m["in_pool"]) for m in top] == [(897, True), (39, False)]


def test_summary_and_profile():
    games = [game(i, champ=897, days_ago=i + 1) for i in range(25)] + [game(100 + i, champ=103, role="mid", days_ago=3) for i in range(5)]
    execution = [ex(i, 0.05) for i in range(6)]
    rank = [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 40}]
    row = summary(games, POOL, execution, "top", rank, NOW_MS)
    assert row["games"] == 25 and row["pool"]["size"] == 4 and row["pending"] == 1 and row["rank"]["label"] == "Gold II"
    assert row["execution"]["label"] == "Wins lane" and row["execution"]["mean_pp"] == pytest.approx(5.0)

    wide = profile(games, POOL, execution, "top", [[897, 250_000, NOW_MS]], rank, "60d", NOW_MS)
    assert wide["tiles"]["games"] == 25 and wide["tiles"]["pool_champs"] == 4
    assert [p["role"] for p in wide["pools"]] == ["top", "mid"]  # their role first, then other roles they have a pool in
    assert [r["role"] for r in wide["roles"]] == ["top", "mid"] and wide["mastery"][0]["in_pool"]
    narrow = profile(games, POOL, execution, "top", None, rank, "20", NOW_MS)
    assert narrow["tiles"]["games"] + sum(r["games"] for r in narrow["roles"] if r["role"] != "top") == 20  # the last 20 games
    assert narrow["mastery"] == [] and narrow["rank"]["flex"] is None
    with pytest.raises(ValueError):
        profile(games, POOL, execution, "top", window="7d")

    nobody = profile([], [], [], "top", now_ms=NOW_MS)  # a player with no data at all still has a page
    assert nobody["tiles"]["games"] == 0 and nobody["tiles"]["kda"] is None and nobody["execution"]["enough"] is False
    assert nobody["pools"] == [{"role": "top", "pool": [], "not_in_pool": []}]


# ---------- filling and backfill ----------
def match(mid, champ, win, k, d, a, lane, jungle, vision, duration=1800):
    return {"metadata": {"matchId": mid}, "info": {"gameDuration": duration, "queueId": 440, "gameStartTimestamp": NOW_MS - DAY_MS,
            "participants": [{"puuid": "api-me", "championId": champ, "teamPosition": "TOP", "win": win, "kills": k, "deaths": d,
                              "assists": a, "totalMinionsKilled": lane, "neutralMinionsKilled": jungle, "visionScore": vision},
                             {"puuid": "x", "championId": 1, "teamPosition": "TOP", "win": not win, "kills": 1, "deaths": 1, "assists": 1,
                              "totalMinionsKilled": 100, "neutralMinionsKilled": 0, "visionScore": 5}]}}


MATCHES = {"EUW1_1": match("EUW1_1", 897, True, 7, 2, 9, 200, 12, 24), "EUW1_2": match("EUW1_2", 150, False, 1, 6, 3, 150, 0, 11, duration=1500)}


def riot_for(tmp_path, calls):
    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/ids"):
            return httpx.Response(200, json=list(MATCHES))
        if "/league/" in req.url.path:
            return httpx.Response(200, json=[{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 40}])
        if "/champion-mastery/" in req.url.path:
            return httpx.Response(200, json=[{"championId": 150, "championPoints": 9000, "lastPlayTime": 5},
                                             {"championId": 897, "championPoints": 250_000, "lastPlayTime": 7}])
        return httpx.Response(200, json=MATCHES[req.url.path.split("/")[-1]])

    return RiotClient("RGAPI-x", tmp_path / "cache", limiter=RateLimiter(sleep=lambda s: None), transport=httpx.MockTransport(handler),
                      sleep=lambda s: None)


def test_game_stats_from_a_match():
    m = MATCHES["EUW1_1"]
    assert game_stats(m["info"]["participants"][0], m["info"]) == {"kills": 7, "deaths": 2, "assists": 9, "cs": 212, "vision": 24,
                                                                  "duration_s": 1800}
    assert game_stats({}, {})["cs"] is None  # an old or odd match: unknown, not 0


def test_a_backfilled_database_gives_the_same_figures_as_fresh_games(tmp_path):
    calls: list[str] = []
    riot = riot_for(tmp_path, calls)
    fresh = init_db(tmp_path / "fresh.sqlite")
    set_players(fresh, [{"role": "top", "riot_id": "Me#EUW"}], {"Me#EUW": "api-me"}.get)
    assert fetch_history(fresh, riot) == {"top": 2}
    with Session(fresh) as s:
        puuid = s.exec(select(Player).where(Player.role == "top")).one().puuid
    want = figures(load_inputs(fresh, puuid).games)
    assert want["stat_games"] == 2 and want["kda"] == pytest.approx((7 + 9 + 1 + 3) / 8)

    # the same games as an older version stored them: no stats. The matches are in the cache from the fetch above.
    old = init_db(tmp_path / "old.sqlite")
    set_players(old, [{"role": "top", "riot_id": "Me#EUW"}], {"Me#EUW": "api-me"}.get)
    with Session(old) as s:
        old_puuid = s.exec(select(Player).where(Player.role == "top")).one().puuid
        for mid, m in MATCHES.items():
            me = m["info"]["participants"][0]
            s.add(PlayerGame(puuid=old_puuid, match_id=mid, champ_id=me["championId"], role="top", win=me["win"], queue_id=440,
                             game_start_ms=m["info"]["gameStartTimestamp"], source="match-v5"))
        s.add(PlayerGame(puuid=old_puuid, match_id="draft-9", champ_id=897, role="top", win=True, game_start_ms=NOW_MS, source="logged"))
        s.add(PlayerGame(puuid=old_puuid, match_id="EUW1_404", champ_id=897, role="top", win=True, game_start_ms=NOW_MS, source="logged"))
        s.commit()
    assert figures(load_inputs(old, old_puuid).games)["stat_games"] == 0
    before = len(calls)
    assert backfill_game_stats(old, riot) == 2 and backfill_game_stats(old, riot) == 0
    assert len(calls) == before  # from the cache: not one request, even for the match that isn't cached
    got = figures([g for g in load_inputs(old, old_puuid).games if g.match_id in MATCHES])
    assert got == want


def test_rank_and_mastery_are_refreshed_at_most_once_a_day(tmp_path, engine):
    calls: list[str] = []
    riot = riot_for(tmp_path, calls)
    set_players(engine, [{"role": "top", "riot_id": "Me#EUW"}], {"Me#EUW": "api-me"}.get)
    now = time.time()
    assert refresh_profiles(engine, riot, now) == 1 and refresh_profiles(engine, riot, now + 3600) == 0
    with Session(engine) as s:
        p = s.exec(select(Player).where(Player.role == "top")).one()
        assert json.loads(p.mastery_json) == [[897, 250_000, 7], [150, 9000, 5]] and p.profile_fetched_at is not None  # highest first
        assert rank_summary(json.loads(p.rank_json))["best"]["label"] == "Gold II"
        puuid = p.puuid
    assert refresh_profiles(engine, riot, now + 90_000) == 1  # a day later
    assert refresh_profiles(engine, riot, now + 90_001, only=puuid) == 1  # asked for by name: whatever its age
    assert refresh_profiles(engine, riot, now + 90_002, only="someone-else") == 0
    inputs = load_inputs(engine, puuid)
    assert inputs.mastery[0][0] == 897 and inputs.rank[0]["tier"] == "GOLD"
    assert mastery_rows([{"championId": 1, "championPoints": 5}, {"championPoints": 9}]) == [[1, 5, 0]]


def test_lane_execution_rows_belong_to_whoever_played_the_role(engine):
    with Session(engine) as s:
        s.add(Player(puuid="me", riot_id="Me#EUW", role="top", rank="", bracket="gold"))
        s.add(Player(puuid="sub", riot_id="Sub#EUW", role="top", rank="", bracket="gold", active=False))
        s.add(PoolEntry(puuid="me", champ_id=897, role="top", manual_tier="main", scored_tier="main", tier_locked=True))
        for i, (who, with_game_id) in enumerate([("me", True), ("me", False), ("sub", True)]):
            d = Draft(game_id=700 + i if with_game_id else None, side="blue", played_at=datetime.now(UTC), result="win", draft_format="tournament")
            s.add(d)
            s.flush()
            mid = f"EUW1_{d.game_id}" if with_game_id else f"draft-{d.draft_id}"
            s.add(PlayerGame(puuid=who, match_id=mid, champ_id=897, role="top", win=True, game_start_ms=NOW_MS - i, source="logged"))
            for role, share in (("top", 0.03 + i / 100), ("mid", 0.5)):
                s.add(GameExecutionRole(draft_id=d.draft_id, role=role, share=share, label="ahead", headline_json="{}", stats_json="[]"))
        s.commit()
    mine = load_inputs(engine, "me")
    assert sorted(r.share for r in mine.execution) == pytest.approx([0.03, 0.04])  # their two games, their role's row only
    assert [r.share for r in load_inputs(engine, "sub").execution] == pytest.approx([0.05])
    assert mine.pool == [PoolRow(897, "top", "main", "main", True)] and mine.mastery is None and mine.rank is None
