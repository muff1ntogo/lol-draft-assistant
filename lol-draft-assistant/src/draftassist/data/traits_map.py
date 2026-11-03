"""Where every measured trait comes from in match-v5 JSON (docs/synergy-v2.md, Stage 1). All field names
live here, so a Riot change breaks one module and its test.

`challenges` keys vary by patch: a missing key gives None (not 0), and the trait built from it is None and
ignored downstream. Checked against a live EUW match (16.21): every key below was present.

Per participant we store raw values (match_participants columns); data/traits.py turns them into per-minute
rates, weighted means and percentiles within the role.
"""
from __future__ import annotations

from collections.abc import Sequence

# match_participants columns filled from each participant
PARTICIPANT_TRAIT_COLUMNS = ("cc_time", "immobilizations", "dmg_taken_share", "self_mitigated", "heal_shield",
                             "lane_adv")

# The traits measured from match stats. dmg_* stay real shares (the damage bar needs them); ranged is 0/1
# from Data Dragon; the rest are percentiles within the role.
MEASURED_TRAITS = ("cc", "frontline", "peel", "dmg_phys", "dmg_magic", "dmg_true", "power_curve",
                   "lane_pressure", "ranged")

# trait -> its sources: (column, per minute?). A trait with several sources is the mean of their
# percentiles, over the sources that have data.
TRAIT_SOURCES: dict[str, tuple[tuple[str, bool], ...]] = {
    "cc": (("cc_time", True), ("immobilizations", True)),
    "frontline": (("dmg_taken_share", False), ("self_mitigated", True)),
    "peel": (("heal_shield", True),),
    "lane_pressure": (("lane_adv", False),),
}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sum(*vals) -> float | None:
    known = [v for v in map(_num, vals) if v is not None]
    return sum(known) if known else None


def parse_traits(p: dict, team: Sequence[dict]) -> dict[str, float | None]:
    """Trait source values for participant `p`; `team` is its five teammates (itself included), for
    the share of damage taken when the challenges share is missing."""
    ch = p.get("challenges") or {}
    share = _num(ch.get("damageTakenOnTeamPercentage"))
    if share is None:
        taken = _num(p.get("totalDamageTaken"))
        total = _sum(*(t.get("totalDamageTaken") for t in team))
        share = taken / total if taken is not None and total else None
    lane = _num(ch.get("laningPhaseGoldExpAdvantage"))
    if lane is None:
        lane = _num(ch.get("earlyLaningPhaseGoldExpAdvantage"))
    return {
        "cc_time": _num(p.get("timeCCingOthers")),
        "immobilizations": _num(ch.get("enemyChampionImmobilizations")),
        "dmg_taken_share": share,
        "self_mitigated": _num(p.get("damageSelfMitigated")),
        "heal_shield": _sum(p.get("totalHealsOnTeammates"), p.get("totalDamageShieldedOnTeammates")),
        "lane_adv": lane,
    }


def parse_match_traits(participants: Sequence[dict]) -> tuple[dict[str, float | None], ...]:
    """Trait source values for every participant, in order."""
    teams: dict[int, list[dict]] = {}
    for p in participants:
        teams.setdefault(p.get("teamId"), []).append(p)
    return tuple(parse_traits(p, teams[p.get("teamId")]) for p in participants)
