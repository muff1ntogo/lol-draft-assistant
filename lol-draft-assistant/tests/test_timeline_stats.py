"""Stage 1 of docs/draft-vs-execution.md: everything taken from a timeline, for crawled games and our own."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from draftassist.data import crawl
from draftassist.data.postgame import fill_own_timelines, verify_draft
from draftassist.data.timeline_map import (
    PARTICIPANT_COLUMNS,
    TEAM_COLUMNS,
    classify_death,
    parse_timeline,
    sanitize_for_fixture,
)
from draftassist.data.timelines import (
    TIMELINE_VERSION,
    backfill_timelines,
    estimate_refetch,
    refetch_sample,
    store_timeline,
)
from draftassist.db.models import (
    CrawledMatch,
    Draft,
    DraftAction,
    MatchParticipant,
    MatchTeamState,
    OwnGameParticipant,
)
from draftassist.engine.model import ROLES

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
MIN = 60_000
FIXTURE = Path(__file__).parents[1] / "fixtures" / "timelines" / "real-sample.json"


def _match():
    parts = [{"participantId": i + 1, "championId": 100 + i, "teamId": 100 if i < 5 else 200,
              "teamPosition": POSITIONS[i % 5], "win": i < 5, "puuid": f"secret-{i}", "riotIdGameName": f"Name{i}"}
             for i in range(10)]
    return {"metadata": {"matchId": "EUW1_1", "participants": [f"secret-{i}" for i in range(10)]},
            "info": {"gameDuration": 1560, "participants": parts}}


def _frame(minute, events=()):
    """Gold 1000 + 400 a minute + 10 x id, plus 3,000 for participant 1 from minute 18. XP 100 a minute + id.
    Minions 5 a minute + id, jungle 2 a minute, and 4 a minute more for the junglers (2 and 7). Level 9 + odd id."""
    frames = {}
    for pid in range(1, 11):
        bonus = 3000 if pid == 1 and minute >= 18 else 0
        frames[str(pid)] = {"totalGold": 1000 + 400 * minute + 10 * pid + bonus, "xp": 100 * minute + pid,
                            "minionsKilled": 5 * minute + pid, "jungleMinionsKilled": 2 * minute + (4 * minute if pid in (2, 7) else 0),
                            "level": 9 + pid % 2}
    return {"timestamp": minute * MIN + (329 if minute else 0), "participantFrames": frames, "events": list(events)}


def _kill(at, killer, victim, *assists):
    return {"type": "CHAMPION_KILL", "timestamp": at, "killerId": killer, "victimId": victim,
            "assistingParticipantIds": list(assists)}


EVENTS = [
    _kill(5 * MIN, 6, 1),  # solo
    _kill(6 * MIN, 6, 1, 7),  # the enemy jungler joined: ganked
    _kill(7 * MIN, 9, 4, 10),  # their bot lane on our ADC: one lane, so other
    _kill(8 * MIN, 9, 4, 10, 7),  # their jungler joined: ganked
    _kill(9 * MIN, 0, 3),  # a tower: other
    _kill(14 * MIN - 1, 6, 1),  # just before 14:00: solo, kill 6 for team 200
    _kill(14 * MIN, 6, 1),  # 14:00 exactly: not counted as before 14, still a kill before 15
    _kill(14 * MIN + 30_000, 1, 6, 2),  # team 100's only kill before 15
    _kill(16 * MIN + 30_000, 7, 3),  # after 15, team 100 not ahead yet
    _kill(20 * MIN + 30_000, 7, 2),  # team 100 ahead by 3,000 at 20:00
    _kill(20 * MIN + 40_000, 2, 7),  # team 200 behind
    {"type": "TURRET_PLATE_DESTROYED", "timestamp": 5 * MIN, "killerId": 1, "teamId": 200},
    {"type": "TURRET_PLATE_DESTROYED", "timestamp": 6 * MIN, "killerId": 0, "teamId": 200},  # minions: team only
    {"type": "TURRET_PLATE_DESTROYED", "timestamp": 14 * MIN + 30_000, "killerId": 1, "teamId": 200},  # after 14:00
    {"type": "TURRET_PLATE_DESTROYED", "timestamp": 16 * MIN, "killerId": 1, "teamId": 200},  # after 15:00
    {"type": "BUILDING_KILL", "timestamp": 10 * MIN, "killerId": 1, "teamId": 200, "buildingType": "TOWER_BUILDING"},
    {"type": "BUILDING_KILL", "timestamp": 17 * MIN, "killerId": 6, "teamId": 100, "buildingType": "TOWER_BUILDING"},
    {"type": "BUILDING_KILL", "timestamp": 23 * MIN, "killerId": 1, "teamId": 200, "buildingType": "INHIBITOR_BUILDING"},
    {"type": "ELITE_MONSTER_KILL", "timestamp": 6 * MIN, "killerTeamId": 200, "monsterType": "HORDE"},
    {"type": "ELITE_MONSTER_KILL", "timestamp": 7 * MIN, "killerTeamId": 200, "monsterType": "HORDE"},
    {"type": "ELITE_MONSTER_KILL", "timestamp": 10 * MIN, "killerTeamId": 100, "monsterType": "DRAGON"},
    {"type": "ELITE_MONSTER_KILL", "timestamp": 14 * MIN, "killerTeamId": 200, "monsterType": "RIFTHERALD"},
    {"type": "ELITE_MONSTER_KILL", "timestamp": 20 * MIN, "killerTeamId": 100, "monsterType": "DRAGON"},
    {"type": "ELITE_MONSTER_KILL", "timestamp": 22 * MIN, "killerTeamId": 100, "monsterType": "BARON_NASHOR"},
    {"type": "WARD_PLACED", "timestamp": 3 * MIN, "creatorId": 5, "wardType": "YELLOW_TRINKET"},
    {"type": "WARD_PLACED", "timestamp": 4 * MIN, "creatorId": 5, "wardType": "CONTROL_WARD"},
    {"type": "WARD_PLACED", "timestamp": 5 * MIN, "creatorId": 5, "wardType": "UNDEFINED"},  # an ability object
    {"type": "WARD_PLACED", "timestamp": 15 * MIN, "creatorId": 5, "wardType": "SIGHT_WARD"},  # too late
    {"type": "WARD_KILL", "timestamp": 6 * MIN, "killerId": 5, "wardType": "SIGHT_WARD"},
    {"type": "WARD_KILL", "timestamp": 7 * MIN, "killerId": 5, "wardType": "UNDEFINED"},
]


def _timeline(minutes=26, events=EVENTS):
    """Events sit in the frame after their minute, as in the real thing."""
    frames = []
    for m in range(minutes + 1):
        low, high = (m - 1) * MIN, m * MIN
        frames.append(_frame(m, [e for e in events if low <= e["timestamp"] < high]))
    return {"info": {"frames": frames}}


@pytest.fixture
def stats():
    return parse_timeline(_timeline(), _match())


# ---------- test 1: hand-checked values for every column ----------
def test_every_participant_column_on_a_hand_worked_game(stats):
    top = stats.participants[100]  # participant 1, against participant 6
    assert top["lane_gold_15"] == (7000 + 10) - (7000 + 60) == -50
    assert top["lane_xp_15"] == 1501 - 1506 == -5
    assert top["lane_cs_14"] == (5 * 14 + 1 + 2 * 14) - (5 * 14 + 6 + 2 * 14) == -5
    assert top["cs_15"] == (5 * 15 + 1) + 2 * 15 == 106
    assert top["jungle_cs_10"] == 20 and top["jungle_cs_15"] == 30
    assert top["level_15"] == 10
    assert top["deaths_solo_14"] == 2 and top["deaths_ganked_14"] == 1 and top["deaths_other_14"] == 0
    assert top["kp_15"] == 1.0  # the team's one kill before 15
    assert top["plates_14"] == 1
    assert top["wards_placed_15"] == 0 and top["wards_killed_15"] == 0

    jungle = stats.participants[101]  # participant 2: the assist on that one kill
    assert jungle["jungle_cs_10"] == 60 and jungle["jungle_cs_15"] == 90 and jungle["kp_15"] == 1.0

    support = stats.participants[104]
    assert support["kp_15"] == 0.0
    assert support["wards_placed_15"] == 2  # not the ability object, not the one at 15:00
    assert support["wards_killed_15"] == 1


def test_every_team_column_on_a_hand_worked_game(stats):
    ours, theirs = stats.teams[100], stats.teams[200]
    gold_15 = lambda ids: sum(1000 + 400 * 15 + 10 * p for p in ids)  # noqa: E731
    assert ours["gold_15"] == gold_15(range(1, 6)) and theirs["gold_15"] == gold_15(range(6, 11))
    gold_25 = lambda ids: sum(1000 + 400 * 25 + 10 * p for p in ids)  # noqa: E731
    assert ours["gold_25"] == gold_25(range(1, 6)) + 3000 and theirs["gold_25"] == gold_25(range(6, 11))
    assert ours["kills_15"] == 1 and theirs["kills_15"] == 6  # the tower's kill is nobody's
    assert ours["towers_15"] == 1 and theirs["towers_15"] == 0  # teamId is the team that lost it
    assert ours["plates_15"] == 3 and theirs["plates_15"] == 0  # the minions' plate counts for the team
    assert ours["dragons_15"] == 1 and theirs["dragons_15"] == 0
    assert ours["grubs_15"] == 0 and theirs["grubs_15"] == 2
    assert ours["herald_15"] == 0 and theirs["herald_15"] == 1
    assert ours["dragons_after_15"] == 1 and ours["barons"] == 1 and theirs["barons"] == 0
    assert ours["towers_after_15"] == 0 and theirs["towers_after_15"] == 1  # inhibitors aren't towers
    assert ours["deaths_while_ahead"] == 1 and theirs["deaths_while_ahead"] == 0


def test_every_column_has_a_value_or_a_reason(stats):
    assert all(set(p) == set(PARTICIPANT_COLUMNS) for p in stats.participants.values())
    assert all(set(t) == set(TEAM_COLUMNS) for t in stats.teams.values())
    assert all(v is not None for p in stats.participants.values() for v in p.values())


# ---------- test 2: death classification ----------
TEAM = {i: (100 if i <= 5 else 200) for i in range(1, 11)}
LANE = {i: POSITIONS[(i - 1) % 5] for i in range(1, 11)}


@pytest.mark.parametrize(("killer", "victim", "assists", "kind"), [
    (6, 1, [], "solo"),
    (7, 1, [], "solo"),  # alone, whoever it is
    (6, 1, [7], "ganked"),
    (7, 1, [8], "ganked"),
    (9, 4, [10], "other"),  # their bot lane on our ADC
    (10, 5, [9], "other"),  # and on our support
    (9, 4, [10, 7], "ganked"),
    (9, 4, [7], "ganked"),
    (0, 3, [], "other"),  # a tower or minions
    (0, 3, [8], "other"),  # one enemy and a tower
    (0, 3, [6, 7], "ganked"),  # a tower finishing what two enemies started
])
def test_death_classes(killer, victim, assists, kind):
    event = _kill(MIN, killer, victim, *assists)
    assert classify_death(event, TEAM, LANE) == kind


def test_ganked_deaths_do_not_move_the_solo_count():
    alone = parse_timeline(_timeline(events=[_kill(5 * MIN, 6, 1)]), _match()).participants[100]
    both = parse_timeline(_timeline(events=[_kill(5 * MIN, 6, 1), _kill(6 * MIN, 6, 1, 7)]), _match()).participants[100]
    assert alone["deaths_solo_14"] == both["deaths_solo_14"] == 1
    assert (alone["deaths_ganked_14"], both["deaths_ganked_14"]) == (0, 1)


# ---------- test 3: the five lanes add up to the team ----------
def test_the_five_gold_leads_sum_to_the_team_difference(stats):
    ours = sum(stats.participants[100 + i]["lane_gold_15"] for i in range(5))
    assert abs(ours - (stats.teams[100]["gold_15"] - stats.teams[200]["gold_15"])) <= 1
    theirs = sum(stats.participants[105 + i]["lane_gold_15"] for i in range(5))
    assert theirs == -ours


# ---------- test 4: short games, and what can't be known ----------
def test_a_game_under_15_minutes_has_nothing():
    assert parse_timeline(_timeline(minutes=14), _match()) is None
    assert parse_timeline(None, _match()) is None


def test_a_game_ending_before_25_minutes_has_no_gold_at_25():
    stats = parse_timeline(_timeline(minutes=20), _match())
    assert stats.teams[100]["gold_25"] is None and stats.teams[100]["gold_15"] is not None


def test_missing_values_are_null_never_zero():
    timeline = _timeline()
    for frame in timeline["info"]["frames"]:
        del frame["events"]
        del frame["participantFrames"]["1"]["jungleMinionsKilled"]
    stats = parse_timeline(timeline, _match())
    top = stats.participants[100]
    assert top["deaths_solo_14"] is None and top["kp_15"] is None and top["plates_14"] is None
    assert top["wards_placed_15"] is None and stats.teams[100]["towers_15"] is None
    assert top["cs_15"] is None and top["jungle_cs_15"] is None and top["lane_cs_14"] is None
    assert stats.participants[105]["lane_cs_14"] is None  # the other half of that lane
    assert top["lane_gold_15"] == -50  # what is there is still read
    assert stats.participants[101]["cs_15"] == (5 * 15 + 2) + 6 * 15


def test_a_team_with_no_kills_has_no_kill_participation():
    stats = parse_timeline(_timeline(events=[_kill(5 * MIN, 6, 1)]), _match())
    assert stats.participants[100]["kp_15"] is None
    assert stats.participants[105]["kp_15"] == 1.0
    assert stats.participants[106]["kp_15"] == 0.0


def test_a_lane_without_both_positions_has_no_lead():
    match = _match()
    match["info"]["participants"][0]["teamPosition"] = ""
    stats = parse_timeline(_timeline(), match)
    assert stats.participants[100]["lane_gold_15"] is None and stats.participants[105]["lane_gold_15"] is None
    assert stats.participants[101]["lane_gold_15"] is not None


# ---------- storing ----------
def _stored_match(engine, match_id="EUW1_1", bracket="gold", start=0):
    with Session(engine) as s:
        s.add(CrawledMatch(match_id=match_id, patch="16.21", bracket=bracket, queue_id=420, game_start_ms=start))
        for i in range(10):
            s.add(MatchParticipant(match_id=match_id, champ_id=100 + i, team_id=100 if i < 5 else 200,
                                   role=ROLES[i % 5], win=i < 5))
        s.commit()


def test_storing_fills_the_columns_and_marks_the_version(engine):
    _stored_match(engine)
    store_timeline(engine, "EUW1_1", _timeline(), _match())
    with Session(engine) as s:
        top = s.get(MatchParticipant, ("EUW1_1", 100))
        assert top.lane_gold_15 == -50 and top.deaths_solo_14 == 2 and top.kp_15 == 1.0
        team = s.get(MatchTeamState, ("EUW1_1", 200))
        assert team.grubs_15 == 2 and team.kills_15 == 6
        match = s.get(CrawledMatch, "EUW1_1")
        assert match.timeline_done and match.timeline_version == TIMELINE_VERSION


def test_a_short_game_is_marked_done_with_nulls(engine):
    _stored_match(engine)
    store_timeline(engine, "EUW1_1", _timeline(minutes=12), _match())
    with Session(engine) as s:
        assert s.get(MatchParticipant, ("EUW1_1", 100)).lane_gold_15 is None
        assert s.get(MatchParticipant, ("EUW1_1", 100)).cs_15 is None
        assert s.get(MatchTeamState, ("EUW1_1", 100)) is None
        match = s.get(CrawledMatch, "EUW1_1")
        assert match.timeline_done and match.timeline_version == TIMELINE_VERSION  # no reason to fetch it again


def test_a_missing_timeline_is_done_but_can_be_fetched_again(engine):
    _stored_match(engine)
    store_timeline(engine, "EUW1_1", None, _match())
    with Session(engine) as s:
        match = s.get(CrawledMatch, "EUW1_1")
        assert match.timeline_done and match.timeline_version == 0


# ---------- the refetch ----------
def _old(engine, n=8, bracket="gold", version=0):
    with Session(engine) as s:
        for i in range(n):
            s.add(CrawledMatch(match_id=f"EUW1_{bracket}{i}", patch="16.21", bracket=bracket, queue_id=420, game_start_ms=i,
                               timeline_done=True, timeline_version=version))
        s.commit()


def test_the_sample_is_a_share_of_each_bracket(engine):
    _old(engine, 8, "gold")
    _old(engine, 4, "iron")
    sample = refetch_sample(engine, 0.25)
    assert len(sample) == 3 and sum("gold" in m for m in sample) == 2 and sum("iron" in m for m in sample) == 1
    assert refetch_sample(engine, 0.25) == sample  # the same sample every time, so a restart carries on


def test_a_share_counts_what_is_already_done(engine):
    _old(engine, 8, "gold")
    with Session(engine) as s:
        for match_id in refetch_sample(engine, 0.25):
            s.get(CrawledMatch, match_id).timeline_version = TIMELINE_VERSION
        s.commit()
    assert refetch_sample(engine, 0.25) == []  # already a quarter
    assert len(refetch_sample(engine, 1.0)) == 6


def test_the_sample_includes_matches_that_never_had_a_timeline(engine):
    _stored_match(engine, "EUW1_new")
    assert refetch_sample(engine, 1.0) == ["EUW1_new"]


def test_the_estimate_counts_one_call_for_each_timeline_and_one_for_each_uncached_match(engine, tmp_path):
    estimate = estimate_refetch(["EUW1_a", "EUW1_b"], tmp_path)
    assert estimate == {"matches": 2, "requests": 4, "hours": pytest.approx(4 / 3000)}


class _Riot:
    def __init__(self, timeline=None, match=None):
        self.calls, self._timeline, self._match = [], timeline if timeline is not None else _timeline(), match or _match()

    def timeline(self, match_id):
        self.calls.append(match_id)
        return self._timeline

    def match(self, match_id):
        return self._match


def test_a_backfill_can_be_given_its_list(engine, tmp_path):
    _stored_match(engine, "EUW1_1")
    _stored_match(engine, "EUW1_2", start=1)
    riot = _Riot()
    assert backfill_timelines(engine, riot, tmp_path, ids=["EUW1_1"]) == {"updated": 1, "left": 0}
    assert riot.calls == ["EUW1_1"]


def test_the_refetch_prints_its_cost_before_the_first_request(engine, tmp_path, monkeypatch, capsys):
    from draftassist.config import Settings

    main_path = Path(engine.url.database)
    _stored_match(engine)
    with Session(engine) as s:
        from draftassist.db.models import Champion

        s.add(Champion(champ_id=1, key="C1", name="C1", patch="16.21.1"))
        s.commit()
    order = []

    class Riot(_Riot):
        def timeline(self, match_id):
            order.append(("request", "estimated hours" in capsys.readouterr().out))
            return super().timeline(match_id)

    monkeypatch.setattr(crawl, "load_settings", lambda: Settings(riot_api_key="k", db_path=main_path, cache_dir=tmp_path))
    monkeypatch.setattr(crawl, "RiotClient", lambda key, cache: Riot())
    assert crawl.main(["--timelines-refetch", "--share", "1"]) == 0
    assert order == [("request", True)]
    with Session(engine) as s:
        assert s.get(CrawledMatch, "EUW1_1").timeline_version == TIMELINE_VERSION


@pytest.mark.parametrize("share", ["0", "1.5", "-1"])
def test_a_share_must_be_a_fraction(share):
    with pytest.raises(SystemExit):
        crawl.main(["--timelines-refetch", "--share", share])


def test_a_share_needs_the_refetch():
    with pytest.raises(SystemExit):
        crawl.main(["--share", "0.5", "--max-matches", "1"])


# ---------- our own games ----------
def _draft(engine, game_id=1, result="win"):
    with Session(engine) as s:
        draft = Draft(game_id=game_id, side="blue", played_at=datetime.now(UTC), result=result, queue_id=440)
        s.add(draft)
        s.commit()
        draft_id = draft.draft_id
        for i in range(5):
            s.add(DraftAction(draft_id=draft_id, seq=i, side="blue", type="pick", champ_id=100 + i))
        s.commit()
    return draft_id


def test_verifying_a_game_stores_its_timeline_outside_the_crawl_tables(engine):
    draft_id = _draft(engine)
    assert verify_draft(engine, _Riot(), draft_id) == "verified"
    with Session(engine) as s:
        rows = s.exec(select(OwnGameParticipant).where(OwnGameParticipant.draft_id == draft_id)).all()
        assert len(rows) == 10
        top = s.get(OwnGameParticipant, (draft_id, 100))
        assert (top.match_id, top.team_id, top.role, top.lane_gold_15) == ("EUW1_1", 100, "top", -50)
        assert s.get(MatchTeamState, ("EUW1_1", 100)).dragons_15 == 1
        assert s.get(Draft, draft_id).timeline_version == TIMELINE_VERSION
        assert s.exec(select(MatchParticipant)).all() == [] and s.exec(select(CrawledMatch)).all() == []


def test_no_identity_is_stored_with_our_games(engine):
    draft_id = _draft(engine)
    verify_draft(engine, _Riot(), draft_id)
    with engine.connect() as conn:
        for table in ("own_game_participants", "match_team_state"):
            for row in conn.execute(text(f"SELECT * FROM {table}")):
                assert not any("secret" in str(v) or "Name" in str(v) for v in row)


def test_a_timeline_that_fails_does_not_fail_the_verification(engine):
    import httpx

    class Broken(_Riot):
        def timeline(self, match_id):
            raise httpx.ConnectError("down")

    draft_id = _draft(engine)
    assert verify_draft(engine, Broken(), draft_id) == "verified"
    with Session(engine) as s:
        draft = s.get(Draft, draft_id)
        assert draft.match_verified and draft.timeline_version == 0


def test_the_backfill_covers_verified_games_without_a_timeline_once(engine):
    done, waiting = _draft(engine, game_id=1), _draft(engine, game_id=2)
    with Session(engine) as s:
        s.get(Draft, done).match_verified = True
        s.get(Draft, done).timeline_version = TIMELINE_VERSION
        s.get(Draft, waiting).match_verified = True
        s.commit()
    riot = _Riot()
    assert fill_own_timelines(engine, riot) == {waiting: "stored"}
    assert riot.calls == ["EUW1_2"]
    assert fill_own_timelines(engine, riot) == {}


def test_a_timeline_not_there_yet_is_tried_again(engine):
    class NotYet(_Riot):
        def timeline(self, match_id):
            return None

    draft_id = _draft(engine)
    with Session(engine) as s:
        s.get(Draft, draft_id).match_verified = True
        s.commit()
    assert fill_own_timelines(engine, NotYet()) == {draft_id: "not-found"}
    assert fill_own_timelines(engine, _Riot()) == {draft_id: "stored"}


def test_our_short_game_is_done_with_nulls(engine):
    draft_id = _draft(engine)
    verify_draft(engine, _Riot(timeline=_timeline(minutes=12)), draft_id)
    with Session(engine) as s:
        assert s.get(OwnGameParticipant, (draft_id, 100)).lane_gold_15 is None
        assert s.get(Draft, draft_id).timeline_version == TIMELINE_VERSION
        assert s.get(MatchTeamState, ("EUW1_1", 100)) is None


# ---------- a real game ----------
# 38 minutes, 16.21-16.22, sanitised. These numbers were worked out a second way, from the raw game in a separate script
# sharing no code with the parser: all 164 agreed.
def _real():
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_timeline(body["timeline"], body["match"])


REAL_PLAYERS = {  # gold, xp, cs 14, cs 15, jungle 10, jungle 15, level, solo, ganked, other, plates, placed, killed
    420: (549, 1333, 31, 128, 0, 0, 12, 1, 1, 0, 2, 5, 0),
    876: (-149, -431, -1, 126, 67, 110, 11, 0, 3, 0, 0, 2, 2),
    246: (278, -792, -24, 108, 0, 0, 10, 2, 2, 0, 1, 6, 0),
    81: (361, -104, 23, 134, 0, 0, 10, 0, 2, 0, 1, 5, 0),
    412: (640, 938, 15, 28, 0, 0, 8, 1, 1, 0, 1, 6, 6),
    133: (-549, -1333, -31, 104, 0, 0, 11, 1, 1, 0, 1, 5, 0),
    59: (149, 431, 1, 119, 78, 110, 11, 0, 3, 0, 0, 5, 0),
    14: (-278, 792, 24, 131, 0, 0, 11, 0, 1, 0, 3, 5, 0),
    202: (-361, 104, -23, 114, 0, 0, 10, 0, 2, 1, 0, 3, 0),
    57: (-640, -938, -15, 13, 0, 0, 7, 1, 1, 3, 0, 13, 2),
}
REAL_COLUMNS = ("lane_gold_15", "lane_xp_15", "lane_cs_14", "cs_15", "jungle_cs_10", "jungle_cs_15", "level_15",
                "deaths_solo_14", "deaths_ganked_14", "deaths_other_14", "plates_14", "wards_placed_15", "wards_killed_15")
REAL_KP = {420: 3 / 14, 876: 8 / 14, 246: 4 / 14, 81: 8 / 14, 412: 8 / 14, 133: 2 / 13, 59: 9 / 13, 14: 3 / 13, 202: 8 / 13, 57: 7 / 13}
REAL_TEAMS = {
    100: {"gold_15": 30314, "gold_25": 49850, "kills_15": 14, "towers_15": 0, "plates_15": 10, "dragons_15": 1, "grubs_15": 3,
          "herald_15": 0, "dragons_after_15": 0, "barons": 0, "towers_after_15": 3, "deaths_while_ahead": 1},
    200: {"gold_15": 28635, "gold_25": 49580, "kills_15": 13, "towers_15": 0, "plates_15": 5, "dragons_15": 0, "grubs_15": 0,
          "herald_15": 0, "dragons_after_15": 4, "barons": 1, "towers_after_15": 11, "deaths_while_ahead": 3},
}


def test_a_real_game_gives_the_hand_checked_numbers():
    stats = _real()
    for champ, expected in REAL_PLAYERS.items():
        assert tuple(stats.participants[champ][c] for c in REAL_COLUMNS) == expected
        assert stats.participants[champ]["kp_15"] == pytest.approx(REAL_KP[champ])
    assert {t: dict(v) for t, v in stats.teams.items()} == REAL_TEAMS


def test_a_real_games_lanes_sum_to_the_team_gold():
    stats = _real()
    ours = sum(stats.participants[c]["lane_gold_15"] for c in (420, 876, 246, 81, 412))
    assert ours == stats.teams[100]["gold_15"] - stats.teams[200]["gold_15"] == 1679


def test_the_real_fixture_holds_no_identity():
    body = FIXTURE.read_text(encoding="utf-8")
    assert not any(k in body for k in ("puuid", "summonerName", "riotId", "gameId", "matchId", "position", "EUW1"))


# ---------- sanitising ----------
def test_sanitising_leaves_no_identity_and_the_same_numbers():
    clean_timeline, clean_match = sanitize_for_fixture(_timeline(), _match())
    body = json.dumps([clean_timeline, clean_match])
    assert "secret" not in body and "Name" not in body and "puuid" not in body
    assert parse_timeline(clean_timeline, clean_match).participants == parse_timeline(_timeline(), _match()).participants
    assert parse_timeline(clean_timeline, clean_match).teams == parse_timeline(_timeline(), _match()).teams
