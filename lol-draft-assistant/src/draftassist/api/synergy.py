"""Synergy page (docs/synergy-visualizer.md, Stage B): a layered graph of our role pools.

One column per selected role (pool champions by default), pair edges between columns, trios across
three columns whose mean synergy passes the threshold, and locks: a locked champion collapses its
column, and every other node gets a chip with its synergy to each locked champion.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations, product

from draftassist.data.provider import ChampionCatalog
from draftassist.engine.comp import EMPTY_PROFILES, ChampionProfiles, badge
from draftassist.engine.model import ROLE_LABELS, ROLES, OurTeam
from draftassist.engine.synergy import LOW_DATA_GAMES, SynergyTable, to_pp
from draftassist.lcu.parser import LiveDraft

MAX_PER_ROLE = 15
DEFAULT_ROLES = ("jungle", "mid", "support")
DEFAULT_THRESHOLD_PP = 1.0
MAX_TRIOS = 500
TIER_ORDER = {"main": 0, "comfortable": 1, "learning": 2}

Damage = tuple[float, float, float]  # physical, magic, true shares


@dataclass(frozen=True)
class SynergySource:
    table: SynergyTable
    damage: Mapping[tuple[int, str], Damage]  # (champ, role) -> shares
    profiles: ChampionProfiles = EMPTY_PROFILES  # badges (docs/comp-style.md)


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


def _letter(role: str) -> str:
    return ROLE_LABELS[role][0]


def _short(riot_id: str | None) -> str:
    return (riot_id or "").split("#")[0]


def build_synergy_view(src: SynergySource, catalog: ChampionCatalog, team: OurTeam, roles: list[str],
                       columns: Mapping[str, list[int]] | None = None, locks: Mapping[str, int] | None = None,
                       threshold_pp: float = DEFAULT_THRESHOLD_PP, unavailable: Iterable[int] = (),
                       players: Mapping[str, str] | None = None, max_trios: int = MAX_TRIOS) -> dict:
    table, players = src.table, players or {}
    locks = {r: c for r, c in (locks or {}).items() if c}
    unavailable = set(unavailable) - set(locks.values())
    defaults = default_columns(team, roles)
    cols = {r: list(dict.fromkeys((columns or {}).get(r, defaults[r])))[:MAX_PER_ROLE] for r in roles}
    for r, c in locks.items():
        if r in cols:
            cols[r] = [c]  # a lock collapses the column (and adds an off-pool champion)

    def damage(c: int, r: str) -> dict | None:
        d = src.damage.get((c, r))
        return None if d is None else {"phys": round(d[0], 4), "magic": round(d[1], 4), "true": round(d[2], 4)}

    def card(c: int, r: str) -> dict:
        entry = team.entry(c, r)
        return {**catalog.card(c), "role": r, "tier": entry.tier if entry else None, "locked": locks.get(r) == c,
                "unavailable": c in unavailable, "damage": damage(c, r),
                "badge": badge(src.profiles, c, r).as_dict()}

    out_cols = []
    for r in roles:
        nodes = []
        for c in cols[r]:
            node = card(c, r)
            node["chips"] = [] if c in unavailable or node["locked"] else [
                {"role": lr, "label": _letter(lr), "pp": round(to_pp(p.s), 3), "low_data": p.low_data}
                for lr in ROLES if lr != r and (lc := locks.get(lr)) and (p := table.pair(r, c, lr, lc))]
            nodes.append(node)
        out_cols.append({"role": r, "label": ROLE_LABELS[r], "player": players.get(r) or None, "nodes": nodes})

    edges = []
    for ra, rb in combinations(roles, 2):
        for a, b in product(cols[ra], cols[rb]):
            p = table.pair(ra, a, rb, b)
            edges.append({"a": {"role": ra, "id": a}, "b": {"role": rb, "id": b}, "pp": round(to_pp(p.s), 3),
                          "games": p.games, "low_data": p.low_data})

    trios = []
    for trio_roles in combinations(roles, 3):
        options = [[c for c in cols[r] if c not in unavailable] for r in trio_roles]
        for champs in product(*options):
            slots = list(zip(trio_roles, champs))
            t = table.trio(*slots)
            if t.pp >= threshold_pp:
                trios.append((t, slots))
    trios.sort(key=lambda x: (-x[0].mean, [c for _, c in x[1]]))
    out_trios = [{"champs": [{"role": r, "id": c, "name": catalog.card(c)["name"]} for r, c in slots],
                  "pp": round(t.pp, 3), "weakest_pp": round(to_pp(t.weakest), 3),
                  "weak_pair": t.hides_weak_pair(threshold_pp), "players": [_short(players.get(r)) for r, _ in slots]}
                 for t, slots in trios[:max_trios]]

    locked = sorted(locks.items(), key=lambda kv: ROLES.index(kv[0]))
    pairs = []
    for (ra, a), (rb, b) in combinations(locked, 2):
        p = table.pair(ra, a, rb, b)
        pairs.append({"a": {"role": ra, "id": a}, "b": {"role": rb, "id": b}, "pp": round(to_pp(p.s), 3),
                      "low_data": p.low_data})
    return {
        "window": table.window, "bracket": table.bracket, "low_data_games": LOW_DATA_GAMES,
        "threshold_pp": threshold_pp, "columns": out_cols, "edges": edges, "trios": out_trios,
        "trios_total": len(trios),
        "locked_team": {"champs": [card(c, r) for r, c in locked], "pairs": pairs,
                        "total_pp": round(sum(p["pp"] for p in pairs), 3)},
    }
