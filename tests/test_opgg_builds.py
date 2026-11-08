"""docs/top-lane-power.md, Stage 1: OP.GG builds and matchup guides for the Strategy tab. The fixtures are real OP.GG
responses (8 Nov 2026), public champion statistics with no player data."""
from pathlib import Path

import pytest

from draftassist.data.opgg_builds import (
    OpggBuilds,
    normalise_analysis,
    normalise_matchup,
    opgg_name,
    opgg_tier,
    parse_compact,
)

FIX = Path(__file__).resolve().parent / "fixtures_opgg"
ANALYSIS = (FIX / "analysis_jax_top_gold.txt").read_text(encoding="utf-8")
MATCHUP = (FIX / "matchup_jax_camille_top.json").read_text(encoding="utf-8")


def test_names_and_tiers():
    assert opgg_name("Lee Sin") == "LEE_SIN" and opgg_name("Kai'Sa") == "KAISA" and opgg_name("Dr. Mundo") == "DR_MUNDO"
    assert opgg_name("Nunu & Willump") == "NUNU_WILLUMP" and opgg_name("Jax") == "JAX"
    assert opgg_tier("gold") == "gold" and opgg_tier("all") is None and opgg_tier(None) is None


def test_compact_repr():
    raw = 'class A: x,ys\nclass B: n\n\nA(1,[B(2),B(-3.5)])'
    assert parse_compact(raw) == {"x": 1, "ys": [{"n": 2}, {"n": -3.5}]}
    assert parse_compact('class A: s,t\n\nA("say \\"hi\\"",true)') == {"s": 'say "hi"', "t": True}


def test_real_analysis():
    a = normalise_analysis(parse_compact(ANALYSIS))
    assert a["core"]["names"][0] == "Trinity Force" and a["core"]["games"] > 100 and 0 < a["core"]["wr"] < 1
    assert a["boots"]["names"] and a["starter"]["names"] and len(a["fourth"]) == 3
    assert a["runes"]["keystone"] and a["runes"]["primary"] and a["runes"]["secondary_runes"]
    assert len(a["skill_order"]) == 15 and sorted(a["skill_max"]) == ["E", "Q", "W"]
    assert a["spells"]["ids"] == [4, 14]


def test_real_matchup_guide():
    m = normalise_matchup(MATCHUP)
    assert m["me"] == "Jax" and m["opponent"] == "Camille" and m["tip"]
    assert m["play_style"] and m["lane_advantage"] and m["core"]["names"] and m["skill_order"]
    assert normalise_matchup("not json") is None


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("down")
        return ANALYSIS if name == "lol_get_champion_analysis" else MATCHUP

    def close(self):
        pass


def test_fetched_in_the_background_cached_and_refreshes_the_screen(tmp_path):
    client = FakeClient()
    ready = []
    ob = OpggBuilds(tmp_path, client_factory=lambda: client, on_ready=lambda: ready.append(1))
    assert ob.analysis("Jax", "top", "gold") is None  # not in yet: never blocks
    assert ob.matchup("Jax", "Camille", "top") is None
    assert ob.wait()
    assert ob.analysis("Jax", "top", "gold")["core"]["names"][0] == "Trinity Force"
    assert ob.matchup("Jax", "Camille", "top")["tip"]
    assert len(ready) == 2
    name, args = client.calls[0]
    assert args["tier"] == "gold" and args["champion"] == "JAX" and args["position"] == "top"
    ob2 = OpggBuilds(tmp_path, client_factory=lambda: pytest.fail("cached: no fetch"))
    assert ob2.analysis("Jax", "top", "gold") is not None  # from the disk cache


def test_a_failure_isnt_retried_in_the_same_run(tmp_path):
    client = FakeClient(fail=True)
    ob = OpggBuilds(tmp_path, client_factory=lambda: client)
    ob.analysis("Jax", "top", "gold")
    assert ob.wait()
    ob.analysis("Jax", "top", "gold")
    assert ob.wait() and len(client.calls) == 1


def test_strategy_shows_opgg(make_services, tmp_path):
    from draftassist.api.strategy import StrategyBuilds
    from draftassist.engine.formats import TOURNAMENT
    from draftassist.lcu.demo import demo_sessions

    svc = make_services()
    names = {c: n for c, (_, n) in svc.live.catalog.champs.items()}
    ob = OpggBuilds(tmp_path, client_factory=FakeClient)
    builds = StrategyBuilds(None, tmp_path, "16.21", {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")},
                            None, names, ob)
    svc.live.strategy_builds = type("B", (), {"__call__": lambda self, *a: None, "opgg_for": builds.opgg_for})()
    last = demo_sessions(svc.live.engine, TOURNAMENT, "blue", game_id=3)[-1]
    first = svc.live.view(last)["strategy"]["players"][0]["opgg"]
    assert first["loading"] is True
    assert ob.wait()
    p = svc.live.view(last)["strategy"]["players"][0]["opgg"]
    assert p["analysis"]["core"]["names"] and p["matchup"]["tip"] and p["tier"] == "gold"
    assert p["analysis"]["spells"]["names"] == ["spell 4", "spell 14"]  # no Data Dragon names cached in this test
