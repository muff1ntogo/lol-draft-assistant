"""docs/top-lane-power.md, Stages 2-3: item and skill order from the timelines the crawl already fetches. Event shapes
as checked on a real 16.22 timeline (8 Nov 2026); values here are made up."""
import json

import pytest
from sqlmodel import Session

from draftassist.data.builds import ORDER_MIN_GAMES, load_static, skill_max, summarize_order
from draftassist.data.counts import refresh_counts
from draftassist.data.timeline_map import parse_build_events
from draftassist.data.timelines import store_build_events
from draftassist.db.models import CrawledMatch, MatchParticipant
from draftassist.db.session import init_db
from test_strategy import LUDENS, MERC, MORELLO, SORCS, ZHONYAS, write_static

DORANS_RING, POT, TOME = 1056, 2003, 1052
CHAMP = 103


def event(kind, pid, t, **kw):
    return {"type": kind, "participantId": pid, "timestamp": t * 1000, **kw}


def timeline(events):
    return {"info": {"frames": [{"timestamp": 0, "events": events}]}}


MATCH = {"info": {"participants": [{"participantId": 1, "championId": CHAMP, "teamId": 100},
                                   {"participantId": 2, "championId": 61, "teamId": 200}]}}


def test_events_become_an_ordered_build():
    ev = [event("ITEM_PURCHASED", 1, 5, itemId=DORANS_RING), event("ITEM_PURCHASED", 1, 5, itemId=POT),
          event("SKILL_LEVEL_UP", 1, 10, skillSlot=1, levelUpType="NORMAL"),
          event("ITEM_PURCHASED", 1, 300, itemId=TOME), event("ITEM_UNDO", 1, 305, beforeId=TOME, afterId=0, goldGain=400),
          event("ITEM_UNDO", 1, 306, beforeId=0, afterId=0, goldGain=50),  # a sale undone: no purchase changes
          event("LEVEL_UP", 1, 80, level=2), event("SKILL_LEVEL_UP", 1, 80, skillSlot=3, levelUpType="NORMAL"),
          event("SKILL_LEVEL_UP", 1, 90, skillSlot=4, levelUpType="EVOLVE"),
          event("ITEM_PURCHASED", 1, 600, itemId=LUDENS), event("ITEM_DESTROYED", 1, 600, itemId=TOME),
          event("ITEM_PURCHASED", 2, 9, itemId=POT), event("ITEM_PURCHASED", 0, 0, itemId=3865)]
    out = parse_build_events(timeline(ev), MATCH)
    assert out[CHAMP]["purchases"] == [[DORANS_RING, 5], [POT, 5], [LUDENS, 600]]
    assert out[CHAMP]["skills"] == "QE" and out[CHAMP]["levels"] == {2: 80}
    assert out[61]["purchases"] == [[POT, 9]]


def test_skill_max():
    assert skill_max("QWEQQRQQEREEEWWRWW") == "QEW"
    assert skill_max("QWE") == "QWE" and skill_max("") is None


@pytest.fixture
def db(tmp_path):
    return init_db(tmp_path / "main.sqlite")


def seed(db, n, order, win_every=2, start=0, boots=SORCS, skills="QWEQQRQEQEREEWWRWW"):
    with Session(db) as s:
        for i in range(start, start + n):
            mid = f"O{i}"
            s.add(CrawledMatch(match_id=mid, patch="16.21", bracket="gold", queue_id=420, game_start_ms=i))
            s.flush()
            s.add(MatchParticipant(match_id=mid, champ_id=CHAMP, team_id=100, role="mid", win=i % win_every == 0))
            purchases = [[DORANS_RING, 3], [POT, 3], [boots, 400], [order[0], 640 + i % 10], [order[1], 1100]]
            store_build_events(s.connection(), mid, {CHAMP: {"purchases": purchases, "skills": skills,
                                                             "levels": {6: 400 + i % 20}}})
        s.commit()


def test_ordered_build_from_the_crawl(db, tmp_path):
    write_static(tmp_path)
    static = load_static(tmp_path)
    seed(db, 60, (LUDENS, ZHONYAS))
    seed(db, 20, (ZHONYAS, LUDENS), start=100)
    seed(db, 10, (LUDENS, MORELLO), start=200, boots=MERC)
    refresh_counts(db, "16.21", static)  # Strategy reads the counts (docs/shared-data.md, Stage 2)
    o = summarize_order(db, None, "16.21", CHAMP, "mid", "gold", static)
    assert o["core"]["names"] == ["Luden's Companion", "Zhonya's Hourglass"] and o["core"]["ref_games"] == 60
    assert o["core"]["order_share"] == pytest.approx(60 / 80) and o["core"]["wr"] == pytest.approx(0.5)
    assert o["boots"]["names"] == ["Sorcerer's Shoes"]
    assert o["starter"]["value"] == tuple(sorted((DORANS_RING, POT)))
    assert o["skill_max"]["value"] == "QEW" and o["first3"]["value"] == "QWE"
    assert 640 <= o["first_item_s"] <= 1100 and 400 <= o["level6_s"] < 420


def test_thin_options_are_hidden_and_no_names_means_nothing(db, tmp_path):
    write_static(tmp_path)
    seed(db, ORDER_MIN_GAMES - 1, (LUDENS, ZHONYAS))
    refresh_counts(db, "16.21", load_static(tmp_path))
    o = summarize_order(db, None, "16.21", CHAMP, "mid", "gold", load_static(tmp_path))
    assert o["core"] is None and o["ref_games"] == ORDER_MIN_GAMES - 1
    assert summarize_order(db, None, "16.21", CHAMP, "mid", "gold", load_static(tmp_path / "none")) is None
