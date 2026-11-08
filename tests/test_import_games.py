"""docs/import-games.md: games History missed, imported from match-v5."""
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from draftassist.api.main import create_app
from draftassist.data.import_games import MIN_TEAMMATES, draft_actions, import_missed
from draftassist.db.models import Draft, DraftAction, Player
from draftassist.engine.formats import get_format
from draftassist.engine.state import SKIPPED

NOW = 1_800_000_000.0
ROLES = ["top", "jungle", "mid", "adc", "support"]
POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
OURS = [f"api-{r}" for r in ROLES]
BLUE_CHAMPS = [266, 64, 103, 222, 89]
RED_CHAMPS = [122, 254, 61, 51, 111]


def match(game_id, our_team=100, ours=5, duration=1800, queue=440, start_offset_h=24):
    """Our players are the first `ours` of team `our_team`."""
    parts = []
    for team, champs in ((100, BLUE_CHAMPS), (200, RED_CHAMPS)):
        for i, (champ, pos) in enumerate(zip(champs, POSITIONS)):
            ours_here = team == our_team and i < ours
            parts.append({"puuid": OURS[i] if ours_here else f"other-{team}-{i}", "teamId": team, "championId": champ,
                          "teamPosition": pos, "win": team == 100, "riotIdGameName": f"P{team}{i}"})
    bans = {100: [{"championId": 39, "pickTurn": 3}, {"championId": 114, "pickTurn": 1}, {"championId": -1, "pickTurn": 5}],
            200: [{"championId": 81, "pickTurn": 2}, {"championId": 99, "pickTurn": 4}]}
    return {"info": {"gameDuration": duration, "queueId": queue, "gameVersion": "16.21.123.456",
                     "gameStartTimestamp": int((NOW - start_offset_h * 3600) * 1000), "participants": parts,
                     "teams": [{"teamId": t, "bans": b} for t, b in bans.items()]}}


class FakeRiot:
    def __init__(self, matches, histories):
        self.matches, self.histories = matches, histories
        self.fetched = []

    def account_puuid(self, riot_id):
        return "api-" + riot_id.split("#")[0].lower()

    def match_ids(self, puuid, queue, start_time, count=20, start=0, end_time=None):
        return self.histories.get(puuid, [])[start:start + count]

    def match(self, match_id):
        self.fetched.append(match_id)
        return self.matches.get(match_id)

    def timeline(self, match_id):
        return None


@pytest.fixture
def team(engine):
    with Session(engine) as s:
        for r in ROLES:
            s.add(Player(puuid=f"p-{r}", riot_id=f"{r.capitalize()}#EUW", role=r, rank="", bracket="gold"))
        s.commit()
    return engine


def run(engine, matches, histories=None):
    histories = histories or {p: list(matches) for p in OURS}
    riot = FakeRiot(matches, histories)
    return import_missed(engine, riot, now=NOW), riot


def drafts(engine):
    with Session(engine) as s:
        return s.exec(select(Draft)).all()


def test_a_game_we_played_together_is_imported_with_its_draft(team):
    res, _ = run(team, {"EUW1_101": match(101)})
    assert res.linked == 5 and res.together == 1 and len(res.imported) == 1
    (d,) = drafts(team)
    assert (d.game_id, d.side, d.result, d.queue_id, d.draft_format, d.patch) == (101, "blue", "win", 440, "ranked", "16.21")
    assert d.imported and d.match_verified and d.result_source == "match-v5" and d.game_length_s == 1800
    assert json.loads(d.players_json) == {"top": "P1000", "jungle": "P1001", "mid": "P1002", "adc": "P1003",
                                          "support": "P1004"}
    with Session(team) as s:
        acts = s.exec(select(DraftAction).where(DraftAction.draft_id == d.draft_id).order_by(DraftAction.seq)).all()
    assert len(acts) == len(get_format("ranked").steps)
    blue_bans = [a.champ_id for a in acts if a.type == "ban" and a.side == "blue"]
    assert blue_bans == [114, 39, SKIPPED, SKIPPED, SKIPPED]  # by pick turn; a skipped ban and missing ones are skipped
    assert [a.champ_id for a in acts if a.type == "pick" and a.side == "blue"] == BLUE_CHAMPS
    assert [a.role for a in acts if a.type == "pick" and a.side == "blue"] == ROLES
    assert [a.role for a in acts if a.type == "pick" and a.side == "red"] == ROLES  # match-v5 knows theirs too
    assert all(a.our_rank is None and a.suggestions_json is None for a in acts)


def test_red_side_loss_and_ranked_5s_format(team):
    run(team, {"EUW1_102": match(102, our_team=200, queue=4400)})
    (d,) = drafts(team)
    assert (d.side, d.result, d.draft_format) == ("red", "loss", "tournament")


def test_only_games_with_three_of_us_together(team):
    res, riot = run(team, {"EUW1_1": match(1, ours=2), "EUW1_2": match(2, ours=3)},
                    {OURS[0]: ["EUW1_1", "EUW1_2"], OURS[1]: ["EUW1_1", "EUW1_2"], OURS[2]: ["EUW1_2"]})
    assert MIN_TEAMMATES == 3 and res.together == 1 and [d.game_id for d in drafts(team)] == [2]
    assert riot.fetched == ["EUW1_2"]  # a game with two of us isn't even fetched


def test_games_already_in_history_and_remakes_are_skipped(team):
    with Session(team) as s:
        s.add(Draft(game_id=201, side="blue", played_at=datetime.now(UTC), result="win"))
        # logged live without a game id, 5 minutes into the same game: it is that game
        s.add(Draft(side="blue", played_at=datetime.fromtimestamp(NOW - 30 * 3600 + 300, UTC), result="loss"))
        s.commit()
    res, _ = run(team, {"EUW1_201": match(201), "EUW1_202": match(202, start_offset_h=30),
                        "EUW1_203": match(203, duration=200)})
    assert res.already == 2 and res.skipped == 1 and res.imported == []
    assert sorted(d.game_id for d in drafts(team)) == [201, 202]
    res, _ = run(team, {"EUW1_201": match(201)})
    assert res.imported == []  # running it again changes nothing


def test_too_few_linked_players_imports_nothing(engine):
    with Session(engine) as s:
        s.add(Player(puuid="p-top", riot_id="Top#EUW", role="top", rank="", bracket="gold"))
        s.commit()
    res, riot = run(engine, {"EUW1_1": match(1)})
    assert res.linked == 1 and res.imported == [] and riot.fetched == []


def test_benched_players_dont_count(team):
    with Session(team) as s:
        for r in ("mid", "adc", "support"):
            p = s.get(Player, f"p-{r}")
            p.active = False
            s.add(p)
        s.commit()
    res, _ = run(team, {"EUW1_1": match(1)})
    assert res.linked == 2 and res.imported == []


def test_actions_fill_tournament_steps():
    acts = draft_actions(match(1), 100, "tournament")
    assert len(acts) == 20
    assert [c for s, k, c, _ in acts if k == "ban" and s == "red"] == [81, 99, SKIPPED, SKIPPED, SKIPPED]


def test_history_shows_imported_games(make_services, team):
    run(team, {"EUW1_101": match(101)})
    svc = make_services(db=team)
    with TestClient(create_app(svc, stats_refresh_s=0)) as client:
        (row,) = client.get("/api/history").json()["games"]
        assert row["imported"] is True and len(row["our_champs"]) == 5
        recap = client.get(f"/api/history/{row['draft_id']}")
        assert recap.status_code == 200 and recap.json()["header"]["imported"] is True
