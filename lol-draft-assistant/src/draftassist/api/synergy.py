"""Helpers shared by the Prep page's synergy visualiser (api/prep_view.py, docs/synergy-v2.md): default
columns from our pools and the live draft's locks and unavailable champions. (The separate Synergy page of
docs/synergy-visualizer.md, Stage B, was replaced by the merged Prep page.)"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from draftassist.engine.model import OurTeam
from draftassist.lcu.parser import LiveDraft

MAX_PER_ROLE = 15
TIER_ORDER = {"main": 0, "comfortable": 1, "learning": 2}


def default_columns(team: OurTeam, roles: Iterable[str], limit: int = MAX_PER_ROLE) -> dict[str, list[int]]:
    """Each role's pool: main first, then comfortable, then learning; higher comfort first."""
    out = {}
    for role in roles:
        pool = team.pools.get(role, {})
        out[role] = sorted(pool, key=lambda c: (TIER_ORDER.get(pool[c].tier, 9), -pool[c].comfort, c))[:limit]
    return out


def draft_context(live: LiveDraft, our_roles: Mapping[int, str]) -> tuple[set[int], dict[str, int]]:
    """From the live draft: (unavailable champions, our locked picks by role). Our own picks are locks,
    not greyed out. A pick's role is the client's position, else the engine's role assignment."""
    st = live.state
    locks = {}
    for slot in live.our_slots:
        role = slot.role or our_roles.get(slot.champ_id)
        if slot.champ_id and role:
            locks[role] = slot.champ_id
    unavailable = set(st.unavailable()) - set(st.our_picks)
    return unavailable, locks
