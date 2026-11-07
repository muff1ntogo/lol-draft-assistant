"""docs/draft-vs-execution.md, Stage 6: execution-aware comfort, a hint on the Pools page behind a Settings flag."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REAL_NAMES
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.pools import import_pools, parse_pool_text
from draftassist.data.settings_store import get_engine_settings, save_engine_settings
from draftassist.db.models import Champion, Draft, DraftAction, GameExecution, GameExecutionRole, PoolEntry
from draftassist.learning.execution_comfort import (
    HINT_Z,
    LANE_Z_K,
    execution_hint,
    lane_z_by_entry,
    shrunk_z,
)

T0 = datetime(2026, 11, 1, tzinfo=UTC)
NAMES = {c: (n, n) for c, n in REAL_NAMES.items()}


# ---------- pure ----------
def test_the_mean_shrinks_towards_zero_by_games():
    assert shrunk_z([]) == 0.0
    assert shrunk_z([-2.0]) == pytest.approx(-2.0 / (1 + LANE_Z_K))
    many = [-1.0] * 100
    assert shrunk_z(many) == pytest.approx(-100 / (100 + LANE_Z_K))  # close to the raw mean with many games


def test_wins_but_behind_in_lane():
    h = execution_hint(wr=0.56, zs=[-1.5] * 10)
    assert h["kind"] == "behind_in_lane"
    assert h["text"].startswith("Wins, but behind expectation in lane")
    assert h["games"] == 10 and h["z"] == pytest.approx(shrunk_z([-1.5] * 10))


def test_loses_but_ahead_in_lane():
    h = execution_hint(wr=0.44, zs=[1.2] * 8)
    assert h["kind"] == "ahead_in_lane" and h["text"].startswith("Loses, but ahead of expectation in lane")


def test_no_hint_when_lane_and_results_agree_or_lane_is_near_expectation():
    assert execution_hint(wr=0.56, zs=[1.5] * 10) is None  # wins and ahead
    assert execution_hint(wr=0.44, zs=[-1.5] * 10) is None  # loses and behind
    assert execution_hint(wr=0.56, zs=[-0.2] * 10) is None  # within the threshold
    assert execution_hint(wr=0.56, zs=[]) is None


def test_one_bad_game_is_shrunk_below_the_threshold():
    assert abs(shrunk_z([-2.5])) < HINT_Z
    assert execution_hint(wr=0.6, zs=[-2.5]) is None


def test_wording_is_neutral():
    for h in (execution_hint(0.6, [-2.0] * 9), execution_hint(0.4, [2.0] * 9)):
        assert "fault" not in h["text"].lower() and "blame" not in h["text"].lower()


# ---------- from the database ----------
def logged(s, i, side, picks, gold_z):
    d = Draft(side=side, played_at=T0 - timedelta(hours=i), result="win", draft_format="tournament")
    s.add(d)
    s.flush()
    for seq, (role, champ) in enumerate(picks.items()):
        s.add(DraftAction(draft_id=d.draft_id, seq=seq, side=side, type="pick", champ_id=champ, role=role))
    s.add(DraftAction(draft_id=d.draft_id, seq=99, side="red" if side == "blue" else "blue", type="pick", champ_id=1, role="top"))
    s.add(GameExecution(draft_id=d.draft_id, window="16.22", p0=0.5, p15=0.5, draft=0, early=0, conversion=0.5, verdict="Inconclusive"))
    for role, z in gold_z.items():
        head = {"stat": "gold", "actual": 0, "expected": 0, "sd": 1, "z": z, "label": "as expected"}
        s.add(GameExecutionRole(draft_id=d.draft_id, role=role, share=0.0, label="as expected", headline_json=json.dumps(head),
                                stats_json="[]"))
    return d.draft_id


def test_lane_z_is_read_per_role_and_champion(engine):
    with Session(engine) as s:
        logged(s, 0, "blue", {"top": 266, "jungle": 64}, {"top": -1.0, "jungle": 0.5})
        logged(s, 1, "red", {"top": 266}, {"top": -2.0})
        logged(s, 2, "blue", {"top": 122}, {"top": 1.0})
        d = Draft(side="blue", played_at=T0, result="win")  # draft only: no role rows
        s.add(d)
        s.flush()
        s.add(GameExecution(draft_id=d.draft_id, window="16.22", p0=0.5, draft=0.0))
        s.commit()
    by = lane_z_by_entry(engine)
    assert sorted(by[("top", 266)]) == [-2.0, -1.0]
    assert by[("top", 122)] == [1.0] and by[("jungle", 64)] == [0.5]
    assert ("top", 1) not in by  # the enemy's pick isn't ours


def test_the_flag_is_off_by_default_and_saved(engine):
    assert get_engine_settings(engine)["execution_comfort"] is False
    save_engine_settings(engine, execution_comfort=True)
    assert get_engine_settings(engine)["execution_comfort"] is True


def test_pools_page_shows_the_hint_only_with_the_flag_on(make_services, engine):
    with Session(engine) as s:
        for cid, name in REAL_NAMES.items():
            s.add(Champion(champ_id=cid, key=name.replace("'", "").replace(" ", ""), name=name, patch="16.21.1"))
        for i in range(10):
            logged(s, i, "blue", {"top": 266}, {"top": -1.5})
        s.commit()
    import_pools(engine, parse_pool_text("TOP\nAatrox: main", NAMES))
    with Session(engine) as s:
        e = s.exec(select(PoolEntry).where(PoolEntry.champ_id == 266)).one()
        e.games, e.wins = 20, 13  # winning on it
        s.add(e)
        s.commit()
    svc = make_services(db=engine)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        off = client.get("/api/pools").json()["pools"]["top"][0]
        assert off["execution_hint"] is None
        body = client.get("/api/settings").json()
        assert body["execution_comfort"] is False
        put = {"ban_weights": body["ban_weights"], "ban_declared_intents": False, "execution_comfort": True}
        assert client.put("/api/settings", json=put).status_code == 200
        on = client.get("/api/pools").json()["pools"]["top"][0]
    assert on["execution_hint"]["kind"] == "behind_in_lane" and on["execution_hint"]["games"] == 10
    # a hint only: the tier, its proposal and comfort are what they were with the flag off
    assert (on["manual_tier"], on["scored_tier"], on["proposal"]) == (off["manual_tier"], off["scored_tier"], off["proposal"])
    assert on["comfort"] == off["comfort"]
