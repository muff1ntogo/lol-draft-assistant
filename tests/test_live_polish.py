"""ui-engine-review.md, Stage 5: Live's pick cards say when a pick is contested or rests on thin data, and a hover's
difference from our #1."""
import copy

from draftassist.api.live import CONTESTED
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import ROLES
from draftassist.lcu.parser import parse_session
from draftassist.lcu.demo import demo_sessions


def pick_views(svc):
    for e in demo_sessions(svc.live.engine, TOURNAMENT, "blue", game_id=11):
        v = svc.live.view(e)
        if v["center"]["mode"] == "our_pick":
            yield e, v


def test_pick_cards_carry_contested_and_low_data(make_services):
    svc = make_services()
    seen_contested = False
    for _, v in pick_views(svc):
        for card in v["center"]["cards"]:
            assert "contested" in card and "low_data" in card
            if card["contested"] is not None:
                assert card["contested"] >= CONTESTED
                seen_contested = True
    assert seen_contested


def test_a_hover_shows_its_difference_from_our_first(make_services):
    svc = make_services()
    event, view = next(pick_views(svc))
    top = view["center"]["cards"][0]["champ"]["id"]
    shown = {c["champ"]["id"] for c in view["center"]["cards"]}
    live = parse_session(event)
    role = live.actor_role if live.actor_role in ROLES else None
    ranked = svc.live.engine.suggest_picks(live.state, k=12, role=role)
    other = next(s.champ_id for s in reversed(ranked) if s.champ_id not in shown)
    e = copy.deepcopy(event)
    ours = {m["cellId"] for m in e["myTeam"]}
    actor = None
    for group in e["actions"]:
        for a in group:
            if a["actorCellId"] in ours and a.get("isInProgress") and a["type"] == "pick":
                a["championId"], actor = other, a["actorCellId"]
    assert actor is not None
    for m in e["myTeam"]:
        if m["cellId"] == actor:
            m["championId"] = other
    hc = svc.live.view(e)["center"]["hover_card"]
    assert hc is not None
    assert hc["vs_name"] == svc.live.catalog.card(top)["name"] and isinstance(hc["delta_pp"], float)
    assert hc["delta_pp"] <= 0.5
