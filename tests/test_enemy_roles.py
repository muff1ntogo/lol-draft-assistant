"""Enemy roles are the most likely assignment (from how often each champion is played in each role),
not the one that maximises their win rates: a jungler with a high win rate in its rare top games is
still the jungler."""
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT
from draftassist.engine.model import BracketStats, EngineData, OurTeam, RoleStat
from draftassist.engine.state import SKIPPED, DraftState

GRAVES, DARIUS, OURS = 1, 2, 3
META = {
    GRAVES: {"jungle": RoleStat(0.47, 0.054), "top": RoleStat(0.55, 0.0003)},  # rare top games, high WR
    DARIUS: {"top": RoleStat(0.49, 0.05), "jungle": RoleStat(0.50, 0.005)},
    OURS: {"mid": RoleStat(0.50, 0.05)},
}
TEAM = OurTeam.from_tiers({"mid": {OURS: "main"}}, {r: "gold" for r in ("top", "jungle", "mid", "adc", "support")})


def test_enemy_roles_follow_how_champions_are_played():
    eng = DraftEngine(EngineData({"gold": BracketStats(META)}, {1: "Graves", 2: "Darius", 3: "Ahri"}), TEAM)
    st = DraftState(TOURNAMENT, "blue", (*(SKIPPED,) * 6, OURS, GRAVES, DARIUS))
    assert eng.evaluate(st).their_roles == {GRAVES: "jungle", DARIUS: "top"}
