"""Phase 5 (learning): comfort re-scoring with accept/lock, draft report, style suggestion, prep sheet.
Acceptance: tiers update after each logged game; the report shows predicted win % against the result."""
import json
import math
import time
from datetime import UTC, datetime

import httpx
import pytest
from conftest import REAL_NAMES
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.pools import import_pools, parse_pool_text, set_players
from draftassist.data.riot import RateLimiter, RiotClient
from draftassist.db.models import (
    Champion,
    Draft,
    DraftAction,
    Player,
    PlayerGame,
    PoolEntry,
)
from draftassist.engine.comp import ChampionProfiles, ChampStyle
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import ROLES, tier_comfort
from draftassist.lcu.demo import demo_sessions
from draftassist.learning.comfort import (
    accept,
    comfort_score,
    rescore,
    scored_tier,
    set_locked,
    wr_adj,
)
from draftassist.learning.history import collect_logged, fetch_history
from draftassist.learning.jobs import run_learning
from draftassist.learning.prep import prep_sheet
from draftassist.learning.report import draft_report, model_check
from draftassist.learning.style import classify, set_hand_style, team_summary

NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000


# ---------- comfort formula (spec) ----------
def test_comfort_formula_and_tiers():
    assert wr_adj("main", 0, 0) == pytest.approx(0.53) and wr_adj("learning", 0, 0) == pytest.approx(0.46)
    assert wr_adj("comfortable", 10, 8) == pytest.approx((8 + 10 * 0.5) / 20)
    for tier in ("main", "comfortable", "learning"):
        assert comfort_score(tier, 0, 0) == pytest.approx(tier_comfort(tier))  # continuous with v1
    assert comfort_score("comfortable", 20, 10, games_cap=None) == pytest.approx(0.6 * 0 * 10 + 0.4 * math.log(21))
    # capped (default 10 games): more games stop adding comfort, the win rate still counts every game
    assert comfort_score("comfortable", 20, 10) == pytest.approx(0.4 * math.log(11))
    assert comfort_score("comfortable", 50, 25) == comfort_score("comfortable", 20, 10)
    assert comfort_score("comfortable", 50, 40) > comfort_score("comfortable", 50, 25)
    assert scored_tier("comfortable", 50, 40) == "main"  # tiers count every game
    assert scored_tier("learning", 15, 9) == "main"  # 15+ games and wr_adj >= 50%
    assert scored_tier("learning", 15, 5) == "comfortable"  # 15 games but losing
    assert scored_tier("main", 4, 4) == "learning" and scored_tier("main", 5, 0) == "comfortable"


# ---------- fixtures ----------
def seed_champions(engine, session):
    for cid, name in REAL_NAMES.items():
        session.add(Champion(champ_id=cid, key=name.replace("'", "").replace(" ", ""), name=name,
                             patch="16.21.1", tags=["Fighter"]))
    session.commit()
    return engine


@pytest.fixture
def db(engine, session):
    return seed_champions(engine, session)


def add_games(db, puuid, champ, role, wins, losses, days_ago=1, source="match-v5"):
    with Session(db) as s:
        n = s.exec(select(PlayerGame)).all()
        for i in range(wins + losses):
            s.add(PlayerGame(puuid=puuid, match_id=f"EUW1_{len(n) + i}_{champ}_{days_ago}", champ_id=champ, role=role,
                             win=i < wins, queue_id=420, game_start_ms=NOW_MS - days_ago * DAY_MS, source=source))
        s.commit()


def entry(db, champ, role):
    with Session(db) as s:
        return s.exec(select(PoolEntry).where(PoolEntry.champ_id == champ, PoolEntry.role == role)).one()


# ---------- rescore, accept, lock ----------
def test_rescore_counts_recent_games_in_the_pool_role(db):
    import_pools(db, parse_pool_text("TOP\nK'Sante: learning\nGnar: main", {c: (n, n) for c, n in REAL_NAMES.items()}))
    puuid = "unresolved:top"
    add_games(db, puuid, 897, "top", 12, 4)  # 16 recent games, winning
    add_games(db, puuid, 897, "top", 5, 0, days_ago=70)  # outside the 60-day window
    add_games(db, puuid, 897, "mid", 5, 0)  # another role
    proposals = rescore(db, NOW_MS)
    e = entry(db, 897, "top")
    assert (e.games, e.wins) == (16, 12) and e.scored_tier == "main" and e.last_played is not None
    assert e.comfort == pytest.approx(comfort_score("learning", 16, 12))
    assert [(p.champ_id, p.manual_tier, p.scored_tier) for p in proposals] == [(897, "learning", "main"), (150, "main", "learning")]

    set_locked(db, "top", 150, True)  # a locked tier is never proposed over
    assert [p.champ_id for p in rescore(db, NOW_MS)] == [897]
    assert accept(db, "top") == 1  # accept the role's proposals: K'Sante only, Gnar is locked
    assert entry(db, 897, "top").manual_tier == "main" and entry(db, 150, "top").manual_tier == "main"
    assert rescore(db, NOW_MS) == []


def test_collect_logged_games_and_match_v5_wins(db):
    import_pools(db, parse_pool_text("ADC\nJinx: main", {c: (n, n) for c, n in REAL_NAMES.items()}))
    with Session(db) as s:
        d = Draft(game_id=555, side="blue", played_at=datetime.now(UTC), result="win", draft_format="tournament")
        s.add(d)
        s.flush()
        s.add(DraftAction(draft_id=d.draft_id, seq=7, side="blue", type="pick", champ_id=222))  # role from the pool
        s.commit()
    assert collect_logged(db) == 1 and collect_logged(db) == 0
    with Session(db) as s:
        g = s.exec(select(PlayerGame)).one()
        assert (g.puuid, g.match_id, g.role, g.win, g.source) == ("unresolved:adc", "EUW1_555", "adc", True, "logged")


def test_fetch_history_from_match_v5(db, tmp_path):
    set_players(db, [{"role": "top", "riot_id": "Me#EUW"}], {"Me#EUW": "puuid-me"}.get)

    def match(mid, champ, pos, win, duration=1800):
        return {"metadata": {"matchId": mid}, "info": {"gameDuration": duration, "queueId": 440, "gameStartTimestamp": NOW_MS,
                "participants": [{"puuid": "puuid-me", "championId": champ, "teamPosition": pos, "win": win},
                                 {"puuid": "x", "championId": 1, "teamPosition": "TOP", "win": not win}]}}

    matches = {"EUW1_1": match("EUW1_1", 897, "TOP", True), "EUW1_2": match("EUW1_2", 150, "TOP", False),
               "EUW1_3": match("EUW1_3", 897, "TOP", True, duration=120)}

    def handler(req):
        if req.url.path.endswith("/ids"):
            return httpx.Response(200, json=list(matches))
        return httpx.Response(200, json=matches[req.url.path.split("/")[-1]])

    riot = RiotClient("RGAPI-x", tmp_path, limiter=RateLimiter(sleep=lambda s: None),
                      transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert fetch_history(db, riot) == {"top": 2}  # the remake doesn't count
    with Session(db) as s:
        assert {(g.champ_id, g.role, g.win) for g in s.exec(select(PlayerGame))} == {(897, "top", True), (150, "top", False)}


# ---------- acceptance: tiers update after each logged game ----------
def play(svc, game_id, won):
    svc.live.handle_phase("ChampSelect", None)
    for e in demo_sessions(svc.engine, TOURNAMENT, "blue", game_id=game_id):
        svc.live.handle_session(e)
    svc.live.handle_phase("InProgress", None)
    svc.live.handle_phase("EndOfGame", None)
    draft_id = svc.live.handle_end_of_game({"gameId": game_id, "gameLength": 1700, "teams": [
        {"teamId": 100, "isPlayerTeam": True, "isWinningTeam": won}, {"teamId": 200, "isPlayerTeam": False, "isWinningTeam": not won}]})
    svc.live.handle_phase("Lobby", None)
    return draft_id


def test_tiers_update_after_each_logged_game(make_services, db):
    svc = make_services(db=db)
    first = play(svc, 7_300_000_001, True)
    with Session(db) as s:
        ours = s.exec(select(DraftAction).where(DraftAction.draft_id == first, DraftAction.type == "pick",
                                                 DraftAction.side == "blue")).all()
    picks = {a.role: a.champ_id for a in ours}
    assert set(picks) == set(ROLES)  # roles come from the client
    pool_text = "\n\n".join(f"{r.upper()}\n{REAL_NAMES[c]}: learning" for r, c in picks.items())
    import_pools(db, parse_pool_text(pool_text, {c: (n, n) for c, n in REAL_NAMES.items()}))

    run_learning(db)
    top = entry(db, picks["top"], "top")
    assert (top.games, top.wins, top.scored_tier) == (1, 1, "learning")
    for i in range(4):  # the same draft, four more times: 5 games -> comfortable
        play(svc, 7_300_000_010 + i, i % 2 == 0)
        run_learning(db)
        assert entry(db, picks["top"], "top").games == 2 + i  # one more after every logged game
    top = entry(db, picks["top"], "top")
    assert (top.games, top.wins, top.scored_tier, top.manual_tier) == (5, 3, "comfortable", "learning")
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        pools = client.get("/api/pools").json()["pools"]["top"]
        assert pools[0]["proposal"] == "comfortable" and pools[0]["games"] == 5
        assert client.post("/api/pools/accept", json={"role": "top", "champ_id": picks["top"]}).json() == {"changed": 1}
        assert client.get("/api/pools").json()["pools"]["top"][0]["manual_tier"] == "comfortable"
        assert client.post("/api/pools/lock", json={"role": "top", "champ_id": picks["top"], "locked": True}).json()["locked"]
    assert svc.engine.team.entry(picks["top"], "top").comfort == pytest.approx(comfort_score("comfortable", 5, 3))


# ---------- acceptance: the report shows predicted win % against the result ----------
def test_report_shows_predicted_against_result(make_services, db):
    svc = make_services(db=db)
    draft_id = play(svc, 7_300_000_100, False)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        report = client.get(f"/api/history/{draft_id}").json()["report"]
        check = client.get("/api/model-check").json()
    with Session(db) as s:
        predicted = s.get(Draft, draft_id).pred_win_prob
    assert report["predicted"] == pytest.approx(predicted, abs=1e-4) and report["result"] == "loss"
    assert report["brier"] == pytest.approx(predicted ** 2, abs=1e-4)
    assert ("Called it" in report["verdict"]) == (predicted < 0.5) and f"{predicted:.0%}" in report["verdict"]
    assert len(report["factors"]) == 3
    assert check["games"] == 1 and check["calibration"] is None and "too few" in check["verdict"]


def test_report_prices_deviations_from_our_top_pick(make_services, db):
    svc = make_services(db=db)
    draft_id = play(svc, 7_300_000_200, True)
    with Session(db) as s:  # pretend one of our picks was a lower suggestion that nobody took
        actions = s.exec(select(DraftAction).where(DraftAction.draft_id == draft_id)).all()
        taken = {x.champ_id for x in actions}
        swap = None
        for a in actions:
            shown = json.loads(a.suggestions_json) if a.suggestions_json else None
            if a.type != "pick" or a.side != "blue" or not shown or shown["kind"] != "suggested":
                continue
            free = [(rank, item["champ_id"]) for rank, item in enumerate(shown["items"][1:], start=2)
                    if item["champ_id"] not in taken]
            if free:
                swap = (a, *free[0], shown["items"][0]["champ_id"])
                break
        assert swap is not None, "no free lower suggestion in the demo draft"
        a, rank, alt, top = swap
        a.champ_id, a.our_rank = alt, rank
        s.add(a)
        s.commit()
    report = draft_report(db, svc.engine, draft_id)
    (dev,) = report["deviations"]
    assert dev["picked"] == alt and dev["suggested"] == top and dev["rank"] == rank
    assert isinstance(dev["delta_pp"], float)


def test_model_check_trend_then_calibration(db):
    with Session(db) as s:
        for i in range(60):
            s.add(Draft(game_id=i, side="blue", played_at=datetime(2026, 11, 1, tzinfo=UTC).replace(minute=i % 60),
                        result="win" if i % 3 else "loss", pred_win_prob=0.6 if i % 3 else 0.4))
        s.commit()
    check = model_check(db)
    assert check["games"] == 60 and check["brier"] == pytest.approx(0.16) and check["brier"] < check["coin_flip_brier"]
    assert check["calibration"] and sum(b["games"] for b in check["calibration"]) == 60
    assert len(check["trend"]) == 60 and "better" in check["verdict"]


# ---------- style ----------
def test_style_classification():
    # docs/comp-style.md Stage C: the Live comp panel's rule (more in test_comp_ranking.py)
    def st(ftb, dive, poke, front=False):  # old fixtures as identities: protect, engage, poke
        return ChampStyle((dive, poke, 0.0, ftb, 0.0), front, "tags", False)
    profiles = ChampionProfiles({1: st(0.8, 0.2, 0, True), 2: st(0.6, 0.4, 0, True), 3: st(0.2, 0.8, 0),
                                 4: st(0, 1, 0), 5: st(0.1, 0.2, 0.7), 6: st(0, 0.1, 0.9)}, {})
    assert classify(profiles, [(1, None), (2, None), (5, None)]) == "protect"
    assert classify(profiles, [(3, None), (4, None), (5, None)]) == "engage"
    assert classify(profiles, [(5, None), (6, None), (3, None)]) == "poke"
    assert classify(profiles, [(1, None), (4, None), (6, None)]) == "mixed"


def test_team_summary_suggests_after_three_games(make_services, db):
    svc = make_services(db=db)
    for i in range(3):
        play(svc, 7_300_000_300 + i, True)
    summary = team_summary(db)
    assert summary["recent"]["games"] == 3 and summary["recent"]["wins"] == 3
    assert summary["suggested"] in summary["choices"] and summary["suggestion_note"] is None
    set_hand_style(db, "poke")
    assert team_summary(db)["hand_style"] == "poke"
    with pytest.raises(ValueError):
        set_hand_style(db, "turtle")


# ---------- prep ----------
def test_prep_sheet(make_services, db):
    svc = make_services(db=db)
    sheet = prep_sheet(svc.engine, svc.catalog.card)
    w = svc.engine.cfg.ban_weights
    assert sheet["bans"]["blue"]["weights"]["matchup"] == pytest.approx(w.matchup + 0.5)
    assert sheet["bans"]["red"]["weights"]["threat"] == pytest.approx(w.threat + 0.5)
    assert all(len(side["bans"]) <= 5 for side in sheet["bans"].values())
    assert [r["role"] for r in sheet["best"]] == list(ROLES)
    for r in sheet["best"]:
        scores = [b["score"] for b in r["best"]]
        assert scores == sorted(scores, reverse=True) and len(scores) <= 3
    assert all(b["counter_risk"] <= 0.10 for b in sheet["blind_safe"])
    assert all(len(f["roles"]) >= 2 for f in sheet["flex"])


def test_client_linked_player_gets_api_puuid_for_history(db, tmp_path):
    """The client's PUUID (a plain UUID) can't be used with the Riot API: match-v5 needs the encrypted
    one, looked up from the Riot ID. Champ select keeps recognising the player by the client's."""
    from draftassist.data.pools import resolve_from_session, roles_by_puuid

    client_puuid = "8b537ce5-1c61-5665-a221-11ace3b2a095"
    set_players(db, [{"role": "top", "riot_id": "Me#EUW"}])
    resolve_from_session(db, [{"gameName": "Me", "tagLine": "EUW", "puuid": client_puuid}])
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if "/riot/account/v1/" in req.url.path:
            return httpx.Response(200, json={"puuid": "API-PUUID-ENCRYPTED"})
        if req.url.path.endswith("/ids"):
            assert "API-PUUID-ENCRYPTED" in req.url.path  # never the client's UUID
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    riot = RiotClient("RGAPI-x", tmp_path, limiter=RateLimiter(sleep=lambda s: None),
                      transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert fetch_history(db, riot) == {"top": 0}
    with Session(db) as s:
        p = s.exec(select(Player).where(Player.role == "top")).one()
        assert (p.client_puuid, p.api_puuid) == (client_puuid, "API-PUUID-ENCRYPTED")
    assert roles_by_puuid(db)[client_puuid] == "top"



def test_comfort_cap_setting_drives_engine_and_pools(make_services, db):
    from draftassist.data.settings_store import (
        get_engine_settings,
        save_engine_settings,
    )

    import_pools(db, parse_pool_text("TOP\nK'Sante: main", {c: (n, n) for c, n in REAL_NAMES.items()}))
    add_games(db, "unresolved:top", 897, "top", 20, 10)
    save_engine_settings(db, alpha=1.5, beta=0.3)
    svc = make_services(db=db)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        body = client.get("/api/settings").json()
        assert body["comfort_games_cap"] == 10 and body["alpha"] == 1.5 and body["beta"] == 0.3
        put = {"ban_weights": body["ban_weights"], "ban_declared_intents": False, "comfort_games_cap": 5}
        assert client.put("/api/settings", json=put).status_code == 200
        assert client.put("/api/settings", json={**put, "comfort_games_cap": 0}).status_code == 422
        top = client.get("/api/pools").json()["pools"]["top"][0]
    assert get_engine_settings(db) == {"alpha": 1.5, "beta": 0.3, "comfort_games_cap": 5, "damage_balance": True,
                                       "lane_weight": 0.5, "role_weight_override": {}, "blind_weights": {},
                                       "gate_tau": 0.75, "tree_patch_half_life": 0.0,
                                      "damage_penalty": -0.05, "ban_comp_synergy": False, "comp_coherence": True,
                                          "comp_weight": 0.15}  # alpha/beta kept
    assert top["comfort"] == pytest.approx(round(comfort_score("main", 30, 20, games_cap=5), 2))  # 20W 10L
    assert svc.engine.team.entry(897, "top").comfort == pytest.approx(comfort_score("main", 30, 20, games_cap=5))
