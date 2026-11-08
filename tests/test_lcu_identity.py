"""Ranked 5s: a champ select name without its tag (docs/scouting-research.md)."""
import asyncio
import copy
import json
from pathlib import Path

from draftassist.lcu.identity import SUMMONER_BY_PUUID, TagFiller
from draftassist.lcu.parser import parse_session

FLEX = Path(__file__).resolve().parents[1] / "fixtures" / "lcu" / "flex-ranked-sample"


class FakeLcu:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def get(self, endpoint):
        self.calls.append(endpoint)
        return self.answers.get(endpoint)


def session_with_enemy(name="Enemy", tag=None, puuid="client-puuid-1"):
    s = json.loads((FLEX / "0004-session.json").read_text(encoding="utf-8"))
    m = s["theirTeam"][0]
    m["gameName"], m["puuid"] = name, puuid
    if tag is not None:
        m["tagLine"] = tag
    return s


def test_a_name_without_a_tag_is_not_a_riot_id():
    live = parse_session(session_with_enemy())
    slot = live.their_slots[0]
    assert slot.riot_id is None and slot.game_name == "Enemy"
    assert parse_session(session_with_enemy(tag="EUW")).their_slots[0].riot_id == "Enemy#EUW"


def test_the_tag_is_looked_up_from_the_client_once():
    v2 = SUMMONER_BY_PUUID[0].format(puuid="client-puuid-1")
    lcu = FakeLcu({v2: {"gameName": "Enemy", "tagLine": "1234"}})
    filler = TagFiller()
    session = session_with_enemy()
    before = copy.deepcopy(session)
    filled = asyncio.run(filler.fill(lcu, session))
    assert filled["theirTeam"][0]["tagLine"] == "1234" and session == before  # a copy; the original untouched
    asyncio.run(filler.fill(lcu, session_with_enemy()))
    assert lcu.calls == [v2]  # cached
    assert parse_session(filled).their_slots[0].riot_id == "Enemy#1234"


def test_the_second_endpoint_is_tried_and_failure_is_remembered():
    v1 = SUMMONER_BY_PUUID[1].format(puuid="client-puuid-1")
    lcu = FakeLcu({v1: {"tagLine": "EUW"}})
    assert asyncio.run(TagFiller().fill(lcu, session_with_enemy()))["theirTeam"][0]["tagLine"] == "EUW"
    nothing = FakeLcu({})
    filler = TagFiller()
    out = asyncio.run(filler.fill(nothing, session_with_enemy()))
    asyncio.run(filler.fill(nothing, session_with_enemy()))
    assert "tagLine" not in out["theirTeam"][0] and len(nothing.calls) == len(SUMMONER_BY_PUUID)


def test_nothing_to_do_leaves_the_session_alone():
    s = session_with_enemy(tag="EUW")
    lcu = FakeLcu({})
    assert asyncio.run(TagFiller().fill(lcu, s)) is s and lcu.calls == []
    assert asyncio.run(TagFiller().fill(lcu, None)) is None
