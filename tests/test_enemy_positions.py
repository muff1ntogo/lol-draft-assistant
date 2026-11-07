"""docs/enemy-scouting.md, Stage D: the enemy's roles come from the client's positions when it gives them."""
import copy

import pytest

from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.scouting import EnemyScouting, ScoutedPlayer
from draftassist.engine.state import SKIPPED, DraftState
from draftassist.lcu.demo import demo_sessions
from draftassist.lcu.parser import parse_session

EZREAL, LEE_SIN = 81, 64


def their_pick(champ, positions=()):
    """Blue bans 3 and red bans 3 (skipped), blue picks 1 (skipped), then red picks `champ`."""
    state = DraftState(TOURNAMENT, "blue", their_positions=tuple(positions))
    for _ in range(7):
        state = state.apply(SKIPPED) if state.step.kind == "ban" else state.apply(next_free(state))
    assert state.step.side == "red" and state.step.kind == "pick"
    return state.apply(champ)


def next_free(state):
    return next(c for c in (1, 12, 22) if c not in state.unavailable())


@pytest.fixture
def eng(make_services):
    return make_services().engine


def test_an_off_role_pick_takes_the_role_the_client_gives(eng):
    guessed = eng.evaluate(their_pick(EZREAL)).their_roles[EZREAL]
    assert guessed != "top"  # Ezreal is guessed where the bracket plays him
    told = eng.evaluate(their_pick(EZREAL, [(EZREAL, "top")])).their_roles[EZREAL]
    assert told == "top"


def test_the_given_role_is_filled_for_certain(eng):
    filled = eng.role_filled_probs(their_pick(EZREAL, [(EZREAL, "top")]))
    assert filled["top"] == 1.0 and filled["adc"] == 0.0


def test_positions_of_champions_not_picked_yet_or_clashing_are_ignored(eng):
    state = their_pick(EZREAL, [(LEE_SIN, "jungle"), (EZREAL, "top"), (EZREAL, "mid")])
    assert eng._their_positions(state) == {EZREAL: "top"}


def test_scouting_keeps_the_right_players_pool(eng):
    jungler = ScoutedPlayer("Jungler", "jungle", ((LEE_SIN, 10, 6),), ((LEE_SIN, 10),))
    state = their_pick(EZREAL, [(EZREAL, "top")])
    state = DraftState(state.format, state.our_side, state.actions, scouting=EnemyScouting((jungler,)),
                       their_positions=state.their_positions)
    open_roles = [r for r in ("top", "jungle", "mid", "adc", "support") if r not in eng.evaluate(state).their_roles.values()]
    assert "jungle" in open_roles and state.scouting.owner(LEE_SIN, open_roles) is jungler


def test_the_draft_keeps_its_positions():
    state = DraftState(TOURNAMENT, "blue", their_positions=((EZREAL, "top"),))
    assert state.apply(SKIPPED).their_positions == ((EZREAL, "top"),)
    assert state.with_provisional([5]).their_positions == ((EZREAL, "top"),)


def test_the_parser_reads_their_positions(make_services):
    svc = make_services()
    events = demo_sessions(svc.engine, TOURNAMENT, "blue")
    event = copy.deepcopy(next(e for e in events if parse_session(e).state.their_picks))
    pick = parse_session(event).state.their_picks[0]
    cell = next(m["cellId"] for m in event["theirTeam"] if m["championId"] == pick)
    for m in event["theirTeam"]:
        m["assignedPosition"] = "top" if m["cellId"] == cell else ""
    assert parse_session(event).state.their_positions == ((pick, "top"),)


def test_flex_without_positions_guesses_as_before(eng):
    state = their_pick(EZREAL)
    assert state.their_positions == () and eng._their_positions(state) == {}
