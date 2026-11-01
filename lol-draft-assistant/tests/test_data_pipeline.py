"""Phase 3: rate-limited Riot client, crawler, aggregation with shrinkage, OP.GG bootstrap, coverage.
All offline: Riot and OP.GG are mocked with httpx.MockTransport."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlmodel import Session, select

from draftassist.data import provider as provider_mod
from draftassist.data.aggregate import aggregate, shrink
from draftassist.data.coverage import coverage
from draftassist.data.crawler import Crawler, parse_match, patch_of
from draftassist.data.opgg import McpClient, fetch_lane_meta, import_bootstrap, parse_lane_meta
from draftassist.data.provider import load_engine_inputs
from draftassist.data.riot import RateLimiter, RiotAuthError, RiotClient
from draftassist.db.models import (
    BracketTotal,
    Champion,
    ChampRoleStats,
    CrawledMatch,
    Matchup,
    SeedPlayer,
    Synergy,
)

FAKE_KEY = "RGAPI-test-0000"
ROLES_LCU = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


# ---------- shrinkage ----------
def test_shrinkage_pulls_small_samples_to_half_and_leaves_large_ones():
    assert shrink(3, 4, 200) == pytest.approx(103 / 204)  # 75% on 4 games -> ~50.5%
    assert shrink(6_000, 10_000, 200) == pytest.approx(0.598, abs=0.001)  # 60% on 10k games -> 59.8%
    assert shrink(0, 0, 50) == 0.5


# ---------- rate limiter ----------
class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_rate_limiter_enforces_every_window():
    clock = FakeClock()
    rl = RateLimiter(((2, 1.0), (3, 10.0)), clock=clock, sleep=clock.sleep, margin=0)
    for _ in range(3):
        rl.acquire()
    assert clock.t == pytest.approx(1.0)  # third call waited for the 1 s window
    rl.acquire()
    assert clock.t == pytest.approx(10.0)  # fourth waited for the 10 s window


def test_rate_limiter_follows_server_header():
    rl = RateLimiter()
    rl.update_from_header("500:10,30000:600")
    assert rl.limits == ((500, 10.0), (30000, 600.0))
    rl.update_from_header("garbage")
    assert rl.limits == ((500, 10.0), (30000, 600.0))


# ---------- Riot client ----------
def riot_client(tmp_path, handler, clock=None):
    clock = clock or FakeClock()
    return RiotClient(FAKE_KEY, tmp_path, limiter=RateLimiter(clock=clock, sleep=clock.sleep),
                      transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock)


def test_riot_client_sends_key_header_and_caches(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"ok": True}, headers={"X-App-Rate-Limit": "20:1,100:120"})

    c = riot_client(tmp_path, handler)
    assert c.get("europe", "/x", {"a": 1}) == {"ok": True}
    assert c.get("europe", "/x", {"a": 1}) == {"ok": True}
    assert len(calls) == 1 and c.cache_hits == 1
    assert calls[0].headers["X-Riot-Token"] == FAKE_KEY
    assert FAKE_KEY not in repr(c)
    assert not any(FAKE_KEY in p.read_text() for p in (tmp_path / "riot").rglob("*.json"))


def test_riot_client_ttl_and_404(tmp_path):
    clock = FakeClock()
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(404) if req.url.path == "/missing" else httpx.Response(200, json=[len(calls)])

    c = riot_client(tmp_path, handler, clock)
    assert c.get("euw1", "/missing") is None and c.get("euw1", "/missing") is None
    assert calls.count("/missing") == 1  # 404 is cached too
    assert c.get("euw1", "/list", ttl=60) == [2]
    clock.t += 30
    assert c.get("euw1", "/list", ttl=60) == [2]
    clock.t += 31
    assert c.get("euw1", "/list", ttl=60) == [3]


def test_riot_client_retries_429_with_retry_after(tmp_path):
    clock = FakeClock()
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=1)])
    c = riot_client(tmp_path, lambda req: next(responses), clock)
    assert c.get("europe", "/m") == 1
    assert 7 in clock.slept


def test_riot_client_rejected_key_is_a_clear_error_without_the_key(tmp_path):
    c = riot_client(tmp_path, lambda req: httpx.Response(401, json={"status": {"message": "Unknown apikey"}}))
    with pytest.raises(RiotAuthError, match="expire") as e:
        c.get("euw1", "/x")
    assert FAKE_KEY not in str(e.value)
    with pytest.raises(RiotAuthError, match="not set"):
        RiotClient("", tmp_path)


# ---------- matches ----------
def make_match(match_id, blue, red, blue_win=True, version="16.21.712.4501", queue=420, duration=1800,
               bans=((100, 1, 50), (200, 6, 60)), positions=ROLES_LCU):
    participants = []
    for team, champs, win in ((100, blue, blue_win), (200, red, not blue_win)):
        for champ, pos in zip(champs, positions):
            participants.append({"championId": champ, "teamId": team, "teamPosition": pos, "win": win})
    teams = [{"teamId": t, "bans": [{"championId": c, "pickTurn": turn} for tt, turn, c in bans if tt == t]}
             for t in (100, 200)]
    return {"metadata": {"matchId": match_id},
            "info": {"gameVersion": version, "queueId": queue, "gameDuration": duration,
                     "gameStartTimestamp": 1_793_000_000_000, "participants": participants, "teams": teams}}


BLUE = (1, 2, 3, 4, 5)
RED = (11, 12, 13, 14, 15)


def test_parse_match_and_filters():
    m = parse_match(make_match("EUW1_1", BLUE, RED))
    assert m.patch == "16.21" and m.queue_id == 420
    assert (1, 100, "top", True) in m.participants and (15, 200, "support", False) in m.participants
    assert (100, 1, 50) in m.bans
    assert parse_match(make_match("EUW1_2", BLUE, RED, duration=200)) is None  # remake
    assert parse_match(make_match("EUW1_3", BLUE, RED, queue=450)) is None  # ARAM
    assert parse_match(make_match("EUW1_4", BLUE, RED, positions=("TOP", "", "MIDDLE", "BOTTOM", "UTILITY"))) is None
    assert patch_of("16.21.1") == patch_of("16.21.712.4501") == "16.21"


# ---------- crawler + aggregation ----------
def add_champions(session, ids, patch="16.21.1"):
    for c in ids:
        session.add(Champion(champ_id=c, key=f"C{c}", name=f"Champ{c}", patch=patch))
    session.commit()


class FakeRiot:
    """Routes for league-v4, match ids and matches."""

    def __init__(self, matches_by_puuid):
        self.matches_by_puuid = matches_by_puuid
        self.matches = {m["metadata"]["matchId"]: m for ms in matches_by_puuid.values() for m in ms}
        self.calls = []

    def __call__(self, req: httpx.Request):
        path = req.url.path
        self.calls.append(path)
        if "/league/v4/entries/" in path:
            tier, div = path.split("/")[-2:]
            if tier == "GOLD" and div == "I":
                return httpx.Response(200, json=[{"puuid": p, "tier": "GOLD", "rank": "I"} for p in self.matches_by_puuid])
            return httpx.Response(200, json=[])
        if path.endswith("/ids"):
            puuid = path.split("/")[-2]
            queue = int(req.url.params["queue"])
            ids = [m["metadata"]["matchId"] for m in self.matches_by_puuid.get(puuid, [])
                   if m["info"]["queueId"] == queue]
            return httpx.Response(200, json=ids)
        match_id = path.split("/")[-1]
        return httpx.Response(200, json=self.matches[match_id]) if match_id in self.matches else httpx.Response(404)


def test_crawl_and_aggregate_end_to_end(engine, session, tmp_path):
    add_champions(session, BLUE + RED + (50, 60))
    shared = make_match("EUW1_10", BLUE, RED, blue_win=True)
    fake = FakeRiot({
        "p1": [shared, make_match("EUW1_11", BLUE, RED, blue_win=False, queue=440),
               make_match("EUW1_12", BLUE, RED, version="16.20.1.1")],  # previous patch
        "p2": [shared, make_match("EUW1_13", RED, BLUE, blue_win=True, duration=100)],  # remake
    })
    clock = FakeClock()
    clock.t = 1_793_000_000
    riot = riot_client(tmp_path, fake, clock)
    crawler = Crawler(riot, engine, "16.21", brackets=("gold",), clock=clock)
    assert crawler.seed() == 2
    stats = crawler.run(max_matches=100)
    assert stats.stored == {"gold": 2} and stats.skipped_other_patch == 1 and stats.skipped_invalid == 1
    assert stats.players_crawled == 2

    assert aggregate(engine, "16.21") == {"gold": 2}
    with Session(engine) as s:
        assert s.get(BracketTotal, ("16.21", "gold")).matches == 2
        top1 = s.get(ChampRoleStats, ("16.21", "gold", 1, "top"))
        assert (top1.games, top1.wins, top1.picks) == (2, 1, 2)
        assert s.get(ChampRoleStats, ("16.21", "gold", 1, "top")).bans == 0
        mu = s.get(Matchup, ("16.21", "gold", "top", 1, 11))  # canonical a < b
        assert (mu.games, mu.wins_a) == (2, 1)
        syn = s.get(Synergy, ("16.21", "gold", 1, 2))
        assert (syn.games, syn.wins) == (2, 1)
        assert s.get(Champion, 1).roles == ["top"]
        assert len(s.exec(select(SeedPlayer)).all()) == 2

    # idempotent re-aggregation; resumable crawl makes no new match requests
    aggregate(engine, "16.21")
    with Session(engine) as s:
        assert s.get(ChampRoleStats, ("16.21", "gold", 1, "top")).games == 2
    before = len([c for c in fake.calls if "/matches/EUW1_" in c])
    crawler.run(max_matches=100)
    assert len([c for c in fake.calls if "/matches/EUW1_" in c]) == before
    with Session(engine) as s:
        assert len(s.exec(select(CrawledMatch)).all()) == 2


def test_bans_are_counted_once_per_match(engine, session, tmp_path):
    add_champions(session, BLUE + RED + (50,))
    m = make_match("EUW1_20", BLUE, RED, bans=((100, 1, 50), (200, 6, 50)))  # both teams ban 50
    fake = FakeRiot({"p1": [m]})
    riot = riot_client(tmp_path, fake)
    crawler = Crawler(riot, engine, "16.21", brackets=("gold",), clock=lambda: 1_793_000_000)
    crawler.seed()
    crawler.run(max_matches=10)
    aggregate(engine, "16.21")
    with Session(engine) as s:
        # 50 is never picked, so it has no role rows; a picked-and-banned champion would carry bans=1
        assert s.get(ChampRoleStats, ("16.21", "gold", 1, "top")).bans == 0
        rows = s.exec(select(ChampRoleStats).where(ChampRoleStats.champ_id == 50)).all()
        assert rows == []


# ---------- OP.GG ----------
LANE_TOP = (
    "class LolListLaneMetaChampions: lang,position_filter,data\nclass Data: positions\n"
    "class Positions: top\n"
    "class Top: champion,is_rip,play,win,kill,win_rate,pick_rate,role_rate,ban_rate,kda,tier,rank,rank_prev,rank_prev_patch\n\n"
    'LolListLaneMetaChampions("en_US","top",Data(Positions([Top("Champ1",false,1000,520,9000,0.52,0.07,0.78,0.18,2.4,1,1,1,1),'
    'Top("Champ11",false,800,400,8000,0.5,0.05,0.91,0.04,1.8,1,2,2,6),Top("Nobody",false,10,5,1,0.5,0.01,0.5,0.0,1.0,5,99,99,99),'
    'Top("Champ3",false,90,40,800,0.44,0.01,0.05,0.01,1.1,5,60,60,60)])))'
)


def test_parse_lane_meta():
    rows = parse_lane_meta(LANE_TOP, "top")
    assert [r["champion"] for r in rows] == ["Champ1", "Champ11", "Nobody", "Champ3"]
    assert rows[0] == {**rows[0], "role": "top", "play": 1000, "win": 520, "pick_rate": 0.07, "is_rip": False}
    with pytest.raises(ValueError, match="format changed"):
        parse_lane_meta("class Foo: a,b\nFoo(1,2)", "top")


def test_import_bootstrap_matches_names(engine, session):
    add_champions(session, (1, 3, 11))
    stored, unmatched = import_bootstrap(engine, "16.21", parse_lane_meta(LANE_TOP, "top") * 2)
    assert stored == 3 and unmatched == ["Nobody"]


def test_mcp_client_handles_sse_and_session(tmp_path):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((body["method"], req.headers.get("mcp-session-id")))
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                                  headers={"mcp-session-id": "s1"})
        if body["method"] == "tools/call":
            payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": LANE_TOP}]}}
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(payload)}\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(202)

    client = McpClient(transport=httpx.MockTransport(handler))
    raw = fetch_lane_meta(tmp_path, client)
    assert set(raw) == {"top", "jungle", "mid", "adc", "support"} and raw["top"] == LANE_TOP
    assert seen[0] == ("initialize", None) and ("tools/call", "s1") in seen
    assert sum(1 for m, _ in seen if m == "initialize") == 1
    calls = len(seen)
    fetch_lane_meta(tmp_path, client)  # cached for a day
    assert len(seen) == calls


# ---------- provider ----------
def test_provider_uses_bootstrap_then_crawl(engine, session, tmp_path, monkeypatch):
    ids = BLUE + RED
    add_champions(session, ids)
    rows = []
    for i, c in enumerate(ids):
        role = ("top", "jungle", "mid", "adc", "support")[i % 5]
        rows.append({"champion": f"Champ{c}", "role": role, "play": 1000, "win": 520, "pick_rate": 0.1,
                     "ban_rate": 0.05, "role_rate": 0.9})
    import_bootstrap(engine, "16.21", rows)

    inp = load_engine_inputs(engine)
    assert inp.data_source == "opgg" and set(inp.sources.values()) == {"opgg"}
    stat = inp.data.brackets["gold"].role_stat(1, "top")
    assert stat.win_rate == pytest.approx(shrink(520, 1000, 50)) and stat.pick_rate == 0.1
    assert inp.team.entry(1, "top") is not None  # placeholder pool from the most-picked
    assert "placeholder pools" in inp.note

    # a crawled bracket replaces the bootstrap once it has enough matches
    monkeypatch.setattr(provider_mod, "MIN_CRAWL_MATCHES", 2)
    fake = FakeRiot({"p1": [make_match("EUW1_30", BLUE, RED), make_match("EUW1_31", BLUE, RED, blue_win=False)]})
    crawler = Crawler(riot_client(tmp_path, fake), engine, "16.21", brackets=("gold",), clock=lambda: 1_793_000_000)
    crawler.seed()
    crawler.run(max_matches=10)
    aggregate(engine, "16.21")
    inp = load_engine_inputs(engine)
    assert inp.sources == {"iron": "opgg", "gold": "crawl", "emerald": "opgg"} and inp.data_source == "mixed"
    gold = inp.data.brackets["gold"]
    assert gold.role_stat(1, "top").win_rate == pytest.approx(shrink(1, 2, 50))
    assert gold.role_stat(1, "top").pick_rate == pytest.approx(2 / 2)  # picks / matches
    assert gold.matchup_wr("top", 1, 11) == pytest.approx(shrink(1, 2, 200))


def test_provider_falls_back_to_fixture_without_stats(engine, session):
    add_champions(session, (1, 2, 3))
    assert load_engine_inputs(engine).data_source == "fixture"


# ---------- coverage ----------
def test_coverage_report(engine, session):
    add_champions(session, (1, 2, 3))
    import_bootstrap(engine, "16.21", [
        {"champion": "Champ1", "role": "top", "play": 1, "win": 1, "pick_rate": 0.1, "ban_rate": 0, "role_rate": 0.9},
        {"champion": "Champ2", "role": "top", "play": 1, "win": 1, "pick_rate": 0.05, "ban_rate": 0, "role_rate": 0.8},
        {"champion": "Champ3", "role": "top", "play": 1, "win": 1, "pick_rate": 0.01, "ban_rate": 0, "role_rate": 0.05},
    ])
    with Session(engine) as s:
        s.add(BracketTotal(patch="16.21", bracket="gold", matches=5000))
        s.add(ChampRoleStats(patch="16.21", bracket="gold", champ_id=1, role="top", games=900, wins=450, picks=900))
        s.add(ChampRoleStats(patch="16.21", bracket="gold", champ_id=2, role="top", games=150, wins=70, picks=150))
        s.add(Matchup(patch="16.21", bracket="gold", role="top", champ_a=1, champ_b=2, games=40, wins_a=20))
        s.commit()
    cov = coverage(engine, "16.21", brackets=("gold", "iron"))
    gold = cov["gold"]
    assert gold.real_lanes == 2 and gold.covered == 1 and not gold.ok  # Champ3 top isn't a real lane
    assert gold.missing == [(2, "top", 150)]
    assert gold.sparse_matchups == [("top", 1, 2, 40)]
    assert cov["iron"].matches == 0 and cov["iron"].covered == 0
