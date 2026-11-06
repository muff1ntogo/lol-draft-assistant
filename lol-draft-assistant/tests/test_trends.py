"""docs/draft-vs-execution.md, Stage 5: trends on the Team page."""
import json
import math
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from draftassist.api.main import create_app
from draftassist.db.models import Draft, DraftAction, GameExecution, GameExecutionRole
from draftassist.learning.trends import (
    MIN_CHAMP_GAMES,
    MIN_TREND_GAMES,
    Band,
    TrendGame,
    band,
    biggest_lever,
    role_trends,
    team_trend,
)

T0 = datetime(2026, 11, 1, tzinfo=UTC)
SHARES = {"top": 0.0, "jungle": 0.0, "mid": 0.0, "bot": 0.0, "objectives": 0.0}


def game(i, draft=0.0, early=0.0, conversion=0.0, verdict="Inconclusive", shares=None, z=None, champs=None):
    """Parts and shares in pp, newest first by i."""
    return TrendGame(draft_id=i, played_at=T0 - timedelta(hours=i), parts={"draft": draft, "early": early, "conversion": conversion},
                     verdict=verdict, shares={**SHARES, **(shares or {})}, z=z or {}, champs=champs or {})


# ---------- the maths ----------
def test_band_is_the_mean_and_its_standard_error():
    b = band([1.0, 2.0, 3.0, 4.0])
    assert b.mean == pytest.approx(2.5)
    assert b.se == pytest.approx(math.sqrt(sum((x - 2.5) ** 2 for x in [1, 2, 3, 4]) / 3) / 2)  # sample SD / √n
    assert b.n == 4


def test_one_value_has_no_band_and_none_has_no_mean():
    assert band([5.0]) == Band(5.0, None, 1)
    assert band([]) is None


def test_a_band_excludes_zero_only_when_mean_plus_or_minus_one_se_does():
    assert Band(-3.0, 1.0, 10).excludes_zero
    assert Band(2.0, 1.0, 10).excludes_zero
    assert not Band(-1.0, 1.5, 10).excludes_zero
    assert not Band(-1.0, None, 1).excludes_zero


# ---------- team ----------
def test_too_few_games_gives_no_numbers():
    t = team_trend([game(i, draft=-5) for i in range(MIN_TREND_GAMES - 1)], window=None)
    assert t["too_few"] is True and t["games"] == MIN_TREND_GAMES - 1
    assert t["parts"] is None and t["verdicts"] is None and t["lever"] is None


def test_the_window_takes_the_latest_games():
    games = [game(i, draft=-10 if i < 10 else 10) for i in range(25)]
    assert team_trend(games, window=10)["parts"]["draft"]["mean"] == pytest.approx(-10)
    assert team_trend(games, window=10)["games"] == 10
    assert team_trend(games, window=20)["games"] == 20
    assert team_trend(games, window=None)["games"] == 25


def test_team_means_and_verdict_counts():
    games = [game(i, draft=2, early=(-4 if i % 2 else 6), conversion=-20 if i < 3 else 30,
                  verdict="Lost from ahead" if i < 3 else "Inconclusive") for i in range(10)]
    t = team_trend(games, window=None)
    assert t["too_few"] is False
    assert t["parts"]["draft"]["mean"] == pytest.approx(2) and t["parts"]["draft"]["se"] == pytest.approx(0)
    assert t["parts"]["early"]["mean"] == pytest.approx(1)
    assert t["parts"]["conversion"]["mean"] == pytest.approx((3 * -20 + 7 * 30) / 10)
    assert t["verdicts"]["Lost from ahead"] == 3 and t["verdicts"]["Inconclusive"] == 7
    assert sum(t["verdicts"].values()) == 10  # every verdict listed, zeros included


# ---------- biggest lever ----------
def test_the_lever_is_the_most_negative_part_when_its_band_excludes_zero():
    parts = {"draft": Band(-1.0, 3.0, 10), "conversion": Band(-8.0, 2.0, 10), "top": Band(-2.0, 0.5, 10)}
    assert biggest_lever(parts)["part"] == "conversion"


def test_no_lever_when_the_most_negative_band_reaches_zero():
    # top's band excludes 0, but conversion is more negative and its band doesn't: no clear pattern
    parts = {"conversion": Band(-8.0, 9.0, 10), "top": Band(-2.0, 0.5, 10)}
    assert biggest_lever(parts) is None


def test_no_lever_when_nothing_is_negative():
    assert biggest_lever({"draft": Band(3.0, 1.0, 10), "conversion": Band(1.0, 0.2, 10)}) is None


def test_team_lever_reads_role_shares_and_names_them():
    games = [game(i, early=-6, shares={"mid": -5 + (i % 2), "objectives": -1}) for i in range(12)]
    lever = team_trend(games, window=None)["lever"]
    assert lever["part"] == "mid" and lever["name"] == "Mid's early game"
    assert lever["mean"] == pytest.approx(-4.5)


def test_team_lever_none_with_noisy_parts():
    games = [game(i, conversion=(-40 if i % 2 else 38)) for i in range(10)]
    assert team_trend(games, window=None)["lever"] is None


# ---------- per player ----------
def test_per_role_stats_are_mean_z_with_a_band():
    games = [game(i, z={"top": {"gold": 1.0 + (i % 2), "cs": 0.5, "deaths_solo": 0.2}}) for i in range(10)]
    top = next(r for r in role_trends(games, window=None) if r["role"] == "top")
    assert top["games"] == 10 and top["too_few"] is False
    by = {s["stat"]: s for s in top["stats"]}
    assert by["gold"]["mean"] == pytest.approx(1.5) and by["gold"]["se"] > 0
    assert by["deaths_solo"]["lower_is_better"] is True and by["gold"]["lower_is_better"] is False


def test_per_role_too_few_games():
    games = [game(i, z={"mid": {"gold": 1.0}}) for i in range(MIN_TREND_GAMES - 1)]
    mid = next(r for r in role_trends(games, window=None) if r["role"] == "mid")
    assert mid["too_few"] is True and mid["stats"] == [] and mid["champions"] == []


def test_champions_need_five_games():
    games = [game(i, z={"jungle": {"gold": 1.0, "jungle_cs": 0.0, "kp": 0.0}}, champs={"jungle": 64 if i < MIN_CHAMP_GAMES else 254 + i})
             for i in range(12)]
    jungle = next(r for r in role_trends(games, window=None) if r["role"] == "jungle")
    assert [c["champ_id"] for c in jungle["champions"]] == [64]
    assert jungle["champions"][0]["games"] == MIN_CHAMP_GAMES
    assert [s["stat"] for s in jungle["stats"]] == ["gold", "jungle_cs", "kp"]


def test_per_role_reads_only_games_that_have_that_role():
    games = [game(i, z={"support": {"gold": 1.0, "vision": 2.0}}) for i in range(10)] + [game(20 + i) for i in range(5)]
    support = next(r for r in role_trends(games, window=None) if r["role"] == "support")
    assert support["games"] == 10


# ---------- the API ----------
def add_game(s, i, draft_pp, verdict, top_gold_z, champ=266):
    d = Draft(side="blue", played_at=T0 - timedelta(hours=i), result="loss", draft_format="tournament")
    s.add(d)
    s.flush()
    s.add(DraftAction(draft_id=d.draft_id, seq=1, side="blue", type="pick", champ_id=champ, role="top"))
    p0 = 0.5 + draft_pp / 100
    s.add(GameExecution(draft_id=d.draft_id, window="16.22", p0=p0, p15=0.6, draft=p0 - 0.5, early=0.6 - p0, conversion=-0.6,
                        verdict=verdict))
    line = {"stat": "gold", "actual": 0, "expected": 0, "sd": 1, "z": top_gold_z, "label": "as expected"}
    for role, share in {"top": 0.05, "jungle": 0.0, "mid": 0.0, "bot": 0.0, "adc": 0.0, "support": 0.0, "objectives": 0.0}.items():
        s.add(GameExecutionRole(draft_id=d.draft_id, role=role, share=share, label="as expected",
                                headline_json=json.dumps(line), stats_json=json.dumps([line] if role == "top" else [])))


def test_api_trends(make_services, engine):
    with Session(engine) as s:
        for i in range(12):
            add_game(s, i, -3, "Lost from ahead", 1.0 + 0.1 * i)
        # a game with only the draft part doesn't count
        d = Draft(side="blue", played_at=T0, result="win")
        s.add(d)
        s.flush()
        s.add(GameExecution(draft_id=d.draft_id, window="16.22", p0=0.5, draft=0.0))
        s.commit()
    with TestClient(create_app(make_services(db=engine), stats_refresh_s=0)) as client:
        t = client.get("/api/team/trends", params={"window": "10"}).json()
        everything = client.get("/api/team/trends", params={"window": "all"}).json()
        assert client.get("/api/team/trends", params={"window": "7"}).status_code == 422
    assert t["team"]["games"] == 10 and everything["team"]["games"] == 12
    assert t["team"]["parts"]["draft"]["mean"] == pytest.approx(-3)
    assert t["team"]["parts"]["conversion"]["mean"] == pytest.approx(-60)
    assert t["team"]["lever"]["part"] == "conversion"
    top = next(r for r in everything["roles"] if r["role"] == "top")
    assert top["games"] == 12 and top["stats"][0]["stat"] == "gold"
    assert top["champions"][0]["champ"]["id"] == 266
    assert t["windows"] == ["10", "20", "all"] and t["min_games"] == MIN_TREND_GAMES
