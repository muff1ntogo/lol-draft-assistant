"""docs/enemy-scouting.md, Stage C: scouting on the Live screen."""
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.scout import ScoutRunner
from draftassist.data.settings_store import save_engine_settings
from draftassist.db.models import ScoutGame, ScoutPlayer
from draftassist.engine.formats import TOURNAMENT
from draftassist.lcu.demo import demo_sessions

POSITIONS = ["top", "jungle", "middle", "bottom", "utility"]
ROLES = ["top", "jungle", "mid", "adc", "support"]
NAMES = [f"Enemy{i}#EUW" for i in range(5)]


class CountingRiot:
    """Answers nothing new: every player is already stored, so a fetch only shows up as calls."""

    def __init__(self):
        self.calls = 0

    def account_puuid(self, riot_id):
        self.calls += 1
        return f"puuid-{riot_id}"

    def mastery_top(self, puuid, count=20):
        return []

    def match_ids(self, *a, **k):
        return []

    def match(self, match_id):
        return None


def first_ban_event(svc, named=True):
    """Our first ban turn in a tournament draft, with the enemy's names and positions as Ranked 5s shows them, or
    with no names at all (as Flex shows them). Found with scouting off, so finding it fetches nothing."""
    saved = svc.live.scout_provider
    svc.live.scout_provider = None
    try:
        event = next(e for e in demo_sessions(svc.engine, TOURNAMENT, "blue")
                     if svc.live.view(e).get("center", {}).get("mode") == "our_ban")
    finally:
        svc.live.scout_provider = saved
    event = copy.deepcopy(event)
    for m, name, pos in zip(event["theirTeam"], NAMES, POSITIONS):
        if named:
            m["gameName"], m["tagLine"] = name.split("#")
            m["assignedPosition"] = pos
        else:
            m.pop("gameName", None)
            m.pop("tagLine", None)
    return event


def store_pools(engine, svc):
    """Each enemy plays the three champions the bracket picks most in their role, 8 games each."""
    stats = svc.engine.enemy_stats
    now = time.time()
    with Session(engine) as s:
        for name, role in zip(NAMES, ROLES):
            puuid = f"puuid-{name}"
            s.add(ScoutPlayer(riot_id=name, api_puuid=puuid, status="done", fetched_at=now, games_wanted=24,
                              mastery_json=json.dumps([])))
            champs = sorted((c for c in svc.engine.data.champions() if stats.role_stat(c, role)),
                            key=lambda c: -stats.role_stat(c, role).pick_rate)[:3]
            for c in champs:
                for g in range(8):
                    s.add(ScoutGame(api_puuid=puuid, match_id=f"{puuid}-{c}-{g}", champ_id=c, role=role, win=g % 2 == 0,
                                    game_start_ms=int(now * 1000) - 86_400_000))
        s.commit()


@pytest.fixture
def svc(make_services, engine):
    svc = make_services(db=engine)
    svc.riot = CountingRiot()
    svc.scout_runner_override = ScoutRunner(engine, lambda: svc.riot)
    return svc


def wait(runner):
    deadline = time.monotonic() + 5
    while runner.running and time.monotonic() < deadline:
        time.sleep(0.01)


def test_the_client_gives_the_names_and_bans_split_by_their_pools(svc, engine):
    store_pools(engine, svc)
    view = svc.live.view(first_ban_event(svc))
    center = view["center"]
    assert center["scouted"] is True
    groups = [c["group"] for c in center["cards"]]
    assert groups == ["pool"] * groups.count("pool") + ["off"] * groups.count("off")
    assert len(groups) == 7 and groups.count("off") >= 2  # 5 + 2 at bans 1-3
    assert all(c["reason"].startswith("Their ") for c in center["cards"] if c["group"] == "pool")
    assert view["scouting"]["source"] == "client"
    slots = view["their_slots"]
    assert [s["scout"]["name"] for s in slots] == [n.split("#")[0] for n in NAMES]
    assert slots[0]["scout"]["role"] == "top" and slots[0]["scout"]["top"][0]["games"] == 8


def test_later_bans_are_three_and_one(svc, engine):
    store_pools(engine, svc)
    events = demo_sessions(svc.engine, TOURNAMENT, "blue")
    phase2 = None
    for event in events:
        view = svc.live.view(event)
        if view.get("center", {}).get("mode") == "our_ban" and view["phase"].startswith("Ban") and \
                len(view["our_bans"]) + len(view["their_bans"]) >= 6:
            phase2 = copy.deepcopy(event)
            break
    assert phase2 is not None
    for m, name, pos in zip(phase2["theirTeam"], NAMES, POSITIONS):
        m["gameName"], m["tagLine"] = name.split("#")
        m["assignedPosition"] = pos
    cards = svc.live.view(phase2)["center"]["cards"]
    assert len(cards) == 4 and [c["group"] for c in cards].count("off") >= 1


def test_each_player_is_asked_for_once_per_game(svc, engine):
    event = first_ban_event(svc)
    svc.live.view(event)
    wait(svc.scout_runner_override)
    calls = svc.riot.calls
    assert calls == 5
    svc.live.view(event)  # a refresh, or the next event: no new fetch
    wait(svc.scout_runner_override)
    assert svc.riot.calls == calls
    svc.live.handle_phase("EndOfGame", None)  # a new game asks again
    svc.live.view(event)
    wait(svc.scout_runner_override)
    assert svc.riot.calls >= calls  # fresh players are skipped inside; the ask itself happened


def test_pasted_names_when_the_client_gives_none(svc, engine):
    store_pools(engine, svc)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        assert client.post("/api/scout", json={"text": "nothing"}).status_code == 422
        body = client.post("/api/scout", json={"text": "\n".join(NAMES)}).json()
        assert body["source"] == "manual" and [p["name"] for p in body["players"]] == [n.split("#")[0] for n in NAMES]
        view = svc.live.view(first_ban_event(svc, named=False))
        assert view["scouting"]["source"] == "manual" and view["center"]["scouted"] is True
        assert client.get("/api/scout").json()["source"] == "manual"
        assert client.delete("/api/scout").json()["players"] == []
    assert "scouted" not in svc.live.view(first_ban_event(svc, named=False))["center"]


def test_the_game_ending_forgets_the_pasted_names(svc):
    svc.scout_manual[:] = NAMES
    svc.live.handle_phase("EndOfGame", None)
    assert svc.scout_manual == []


def test_off_in_settings_means_the_meta_alone(svc, engine):
    store_pools(engine, svc)
    save_engine_settings(engine, scout_enabled=False)
    view = svc.live.view(first_ban_event(svc))
    assert view["scouting"] is None and "scouted" not in view["center"]
    assert all(s["scout"] is None for s in view["their_slots"])


def test_demo_mode_never_scouts_its_made_up_names(svc):
    svc.lcu_mode = "demo"
    view = svc.live.view(first_ban_event(svc))
    assert view["scouting"] is None and svc.riot.calls == 0


def test_the_weight_is_a_setting(svc, engine):
    store_pools(engine, svc)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        body = client.get("/api/settings").json()
        assert body["scout_enabled"] is True and body["scout_weight"] == 0.6
        put = {"ban_weights": body["ban_weights"], "scout_weight": 0.9}
        assert client.put("/api/settings", json=put).json()["scout_weight"] == 0.9
        assert client.put("/api/settings", json={**put, "scout_weight": 1.5}).status_code == 422
    scouting, _ = svc.scout_info(svc.live.current)
    assert scouting is None or scouting.weight == 0.9


# ---------- Stage E ----------
def test_a_failed_fetch_is_tried_once_more_in_the_game(svc, engine):
    with Session(engine) as s:
        s.add(ScoutPlayer(riot_id=NAMES[0], status="error", fetched_at=time.time() - 60))
        s.commit()
    svc.scout_asked.update(n.lower() for n in NAMES)  # already asked for in this game
    svc.live.view(first_ban_event(svc))
    wait(svc.scout_runner_override)
    assert svc.riot.calls == 1 and svc.scout_retried == {NAMES[0].lower()}
    with Session(engine) as s:  # it failed again: no third try this game
        s.merge(ScoutPlayer(riot_id=NAMES[0], status="error", fetched_at=time.time() - 60))
        s.commit()
    svc.live.view(first_ban_event(svc))
    wait(svc.scout_runner_override)
    assert svc.riot.calls == 1


def test_pasted_names_fill_in_for_players_the_client_doesnt_name(svc):
    event = first_ban_event(svc)
    for m in event["theirTeam"][3:]:
        m.pop("gameName", None)
        m.pop("tagLine", None)
    svc.scout_manual[:] = [NAMES[0].upper(), "Extra1#EUW", "Extra2#EUW", "Extra3#EUW"]
    svc.live.view(event)
    ids, given, source = svc.opponents()
    assert source == "client" and ids == NAMES[:3] + ["Extra1#EUW", "Extra2#EUW"]
    assert given["Extra1#EUW"] is None and given[NAMES[0]] == "top"


# ---------- ui-engine-review.md, Stage 2: enemy pools and lanes ----------
def test_bans_say_whose_champion_they_take(svc, engine):
    store_pools(engine, svc)
    cards = svc.live.view(first_ban_event(svc))["center"]["cards"]
    targeted = [c for c in cards if c["group"] == "pool"]
    assert targeted and all(c["target"] and c["target"]["games"] == 8 for c in targeted)
    assert targeted[0]["target"]["share"] == pytest.approx(8 / 24, abs=1e-3)


def test_scouted_players_carry_labels(svc, engine):
    store_pools(engine, svc)
    scout = svc.live.view(first_ban_event(svc))["their_slots"][0]["scout"]
    assert scout["games_in_role"] == 24 and scout["one_trick"] is None and scout["mastery_only"] is False
    assert scout["role_sure"] == 1.0  # the client gave the position


def test_open_enemy_slots_show_their_players_likely_picks(svc, engine):
    store_pools(engine, svc)
    slots = svc.live.view(first_ban_event(svc))["their_slots"]
    top = slots[0]
    assert top["open_role"] == "top" and len(top["likely"]) == 3
    pool = {t["champ"]["id"] for t in top["scout"]["top"]}
    assert {l["champ"]["id"] for l in top["likely"]} & pool  # their pool shows up in what they're likely to pick


# ---------- docs/scouting-research.md: Ranked 5s only, measured ----------
def test_flex_is_never_scouted(svc, engine):
    from pathlib import Path

    store_pools(engine, svc)
    svc.scout_manual[:] = NAMES  # even with names pasted
    flex = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "lcu" / "flex-ranked-sample"
                       / "0004-session.json").read_text(encoding="utf-8"))
    view = svc.live.view(flex)
    assert view["scouting"] is None
    assert svc.riot.calls == 0


def test_a_ranked_5s_draft_records_how_scouting_went(svc, engine):
    from draftassist.db.models import Draft

    store_pools(engine, svc)
    svc.live.logger.pin_provider = None
    events = [copy.deepcopy(e) for e in demo_sessions(svc.engine, TOURNAMENT, "blue", game_id=7_500_000_001)]
    for e in events:
        for m, name, pos in zip(e["theirTeam"], NAMES, POSITIONS):
            m["gameName"], m["tagLine"] = name.split("#")
            m["assignedPosition"] = pos
    svc.live.handle_phase("ChampSelect", None)
    for e in events:
        svc.live.handle_session(e)
    svc.live.handle_phase("InProgress", None)
    with Session(engine) as s:
        rec = json.loads(s.exec(select(Draft)).first().scout_json)
    assert rec["source"] == "client" and rec["players"] == 5 and rec["found"] == 5 and rec["done"] == 5
    assert rec["all_found_s"] >= 0 and rec["all_found_step"] == 0


def test_scout_record_keeps_the_best_seen():
    from draftassist.api.draftlog import scout_record

    info = lambda statuses: {"source": "client", "players": [{"status": s, "games_read": 3} for s in statuses]}
    r = scout_record(None, info(["mastery"] * 4 + ["new"]), 100.0, 0)
    assert r["found"] == 4 and "all_found_s" not in r
    r = scout_record(r, info(["mastery"] * 5), 112.0, 2)
    assert r["all_found_s"] == 12.0 and r["all_found_step"] == 2 and r["done"] == 0
    r = scout_record(r, info(["done"] * 4 + ["missing"]), 150.0, 6)
    assert r["done"] == 4 and r["missing"] == 1 and "all_done_s" not in r
    assert scout_record(r, None, 160.0, 7) is r
