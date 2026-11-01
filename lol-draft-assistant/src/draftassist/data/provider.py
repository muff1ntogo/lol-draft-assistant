"""Builds the engine's inputs from the database: champion catalog (names, icons), stats, our team.

Stats per bracket come from our crawl once it covers the bracket (MIN_CRAWL_MATCHES), otherwise
from the OP.GG bootstrap (all-tier). Sources are never mixed inside a bracket. Win rates are shrunk
towards 50% here (k = 50 role, 200 matchup/synergy). If neither source exists, everything falls
back to synthetic data over the real champion ids.

Our team comes from imported pools (data/pools.py); before any import, each role gets placeholder
pools (its most-picked champions).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.aggregate import PAIR_K, ROLE_K, shrink
from draftassist.data.ddragon import BASE_URL
from draftassist.data.pools import load_team
from draftassist.db.models import (
    BootstrapRoleStats,
    BracketTotal,
    Champion,
    ChampRoleStats,
    Matchup,
    Synergy,
)
from draftassist.engine.model import ROLES, BracketStats, EngineData, OurTeam, RoleStat
from draftassist.engine.simulate import TEAM_BRACKETS, synthetic_data

log = logging.getLogger(__name__)

BRACKETS = ("iron", "gold", "emerald")
MIN_CRAWL_MATCHES = 1000  # below this a bracket's crawl is too thin to replace the bootstrap
FIXTURE_NOTE = "Synthetic stats and pools (real stats arrive with the Phase 3 crawl, pools with Phase 4)"
PLACEHOLDER_POOL_NOTE = "placeholder pools (each role's most-picked champions) until pools are imported"


@dataclass(frozen=True)
class ChampionCatalog:
    patch: str | None
    champs: Mapping[int, tuple[str, str]]  # champ_id -> (Data Dragon key, display name)

    def card(self, champ_id: int | None) -> dict | None:
        if not champ_id:
            return None
        key, name = self.champs.get(champ_id, (None, f"#{champ_id}"))
        icon = f"{BASE_URL}/cdn/{self.patch}/img/champion/{key}.png" if key and self.patch else None
        return {"id": champ_id, "name": name, "icon": icon}


@dataclass(frozen=True)
class EngineInputs:
    data: EngineData
    team: OurTeam
    catalog: ChampionCatalog
    data_source: str  # "crawl" | "opgg" | "mixed" | "fixture"
    note: str | None
    sources: Mapping[str, str] | None = None  # bracket -> "crawl" | "opgg"


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def load_catalog(db: Engine) -> ChampionCatalog:
    with Session(db) as s:
        rows = s.exec(select(Champion)).all()
    patch = max((r.patch for r in rows), default=None, key=_version_key)
    return ChampionCatalog(patch, {r.champ_id: (r.key, r.name) for r in rows})


def crawl_bracket_stats(s: Session, patch: str, bracket: str) -> BracketStats | None:
    total = s.get(BracketTotal, (patch, bracket))
    if total is None or total.matches < MIN_CRAWL_MATCHES:
        return None
    n = total.matches
    meta: dict[int, dict[str, RoleStat]] = {}
    bans: dict[int, float] = {}
    for r in s.exec(select(ChampRoleStats).where(ChampRoleStats.patch == patch, ChampRoleStats.bracket == bracket)):
        meta.setdefault(r.champ_id, {})[r.role] = RoleStat(shrink(r.wins, r.games, ROLE_K), r.picks / n)
        bans[r.champ_id] = r.bans / n
    matchups = {(m.role, m.champ_a, m.champ_b): shrink(m.wins_a, m.games, PAIR_K)
                for m in s.exec(select(Matchup).where(Matchup.patch == patch, Matchup.bracket == bracket))}
    synergy = {frozenset((y.champ_a, y.champ_b)): shrink(y.wins, y.games, PAIR_K)
               for y in s.exec(select(Synergy).where(Synergy.patch == patch, Synergy.bracket == bracket))}
    return BracketStats(meta, matchups, synergy, bans)


def bootstrap_stats(s: Session, patch: str) -> tuple[BracketStats, str] | None:
    """OP.GG rows for `patch`, or the latest patch that has them."""
    patches = s.exec(select(BootstrapRoleStats.patch).distinct()).all()
    if not patches:
        return None
    use = patch if patch in patches else max(patches, key=_version_key)
    meta: dict[int, dict[str, RoleStat]] = {}
    bans: dict[int, float] = {}
    for r in s.exec(select(BootstrapRoleStats).where(BootstrapRoleStats.patch == use)):
        meta.setdefault(r.champ_id, {})[r.role] = RoleStat(shrink(r.wins, r.games, ROLE_K), r.pick_rate)
        bans[r.champ_id] = r.ban_rate
    return BracketStats(meta, {}, {}, bans), use


def placeholder_team(data: EngineData, brackets: Mapping[str, str] = TEAM_BRACKETS) -> OurTeam:
    """Each role's four most-picked champions in that player's bracket: main, comfortable x2, learning."""
    tiers = {}
    for role in ROLES:
        meta = data.brackets[brackets[role]].meta
        ranked = sorted((c for c in meta if role in meta[c]), key=lambda c: (-meta[c][role].pick_rate, c))
        tiers[role] = dict(zip(ranked[:4], ("main", "comfortable", "comfortable", "learning")))
    return OurTeam.from_tiers(tiers, brackets)


def load_engine_inputs(db: Engine) -> EngineInputs:
    catalog = load_catalog(db)
    names = {c: name for c, (_, name) in catalog.champs.items()}
    if not names:
        log.warning("champions table is empty; run `uv run python -m draftassist.data.ddragon`")
    patch = ".".join(catalog.patch.split(".")[:2]) if catalog.patch else None

    if patch:
        with Session(db) as s:
            brackets: dict[str, BracketStats] = {}
            sources: dict[str, str] = {}
            boot = bootstrap_stats(s, patch)
            for b in BRACKETS:
                crawl = crawl_bracket_stats(s, patch, b)
                if crawl is not None:
                    brackets[b], sources[b] = crawl, "crawl"
                elif boot is not None:
                    brackets[b], sources[b] = boot[0], "opgg"
        if len(brackets) == len(BRACKETS):
            data = EngineData(brackets, names)
            kinds = set(sources.values())
            source = kinds.pop() if len(kinds) == 1 else "mixed"
            per_bracket = ", ".join(f"{b} {sources[b]}" for b in BRACKETS)
            boot_note = f" (OP.GG all-tier, patch {boot[1]})" if "opgg" in sources.values() else ""
            team = load_team(db)
            note = f"Stats: {per_bracket}{boot_note}"
            if team is None:
                team, note = placeholder_team(data), f"{note}; {PLACEHOLDER_POOL_NOTE}"
            elif source == "crawl":
                note = None  # real stats and real pools: nothing to warn about
            return EngineInputs(data, team, catalog, source, note, sources)

    data, team = synthetic_data(names=names or None)
    return EngineInputs(data, load_team(db) or team, catalog, "fixture", FIXTURE_NOTE)
