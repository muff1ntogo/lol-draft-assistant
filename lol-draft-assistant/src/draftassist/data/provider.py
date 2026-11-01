"""Builds the engine's inputs from the database: champion catalog (names, icons), stats, our team.

Stats per bracket blend our crawl with the OP.GG bootstrap (all-tier): OP.GG is the prior each
champion starts from, and the bracket's own games take over as they accumulate (bracket_stats).
Matchups and synergy come from the crawl only. If neither source exists, everything falls back to
synthetic data over the real champion ids.

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
from draftassist.data.crawler import brackets_in_use
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

ROLE_PRIOR_GAMES = ROLE_K  # OP.GG's win rate weighs as much as this many of the bracket's own games
RATE_PRIOR_MATCHES = 1000  # OP.GG's pick/ban rates weigh as much as this many crawled matches
FULL_CRAWL_MATCHES = 20_000  # per bracket: enough for every real lane (coverage target)
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
    data_source: str  # "blend" | "opgg" (no crawl yet) | "crawl" (no OP.GG) | "fixture"
    note: str | None
    sources: Mapping[str, str] | None = None  # bracket -> "blend" | "opgg" | "crawl"


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def load_catalog(db: Engine) -> ChampionCatalog:
    with Session(db) as s:
        rows = s.exec(select(Champion)).all()
    patch = max((r.patch for r in rows), default=None, key=_version_key)
    return ChampionCatalog(patch, {r.champ_id: (r.key, r.name) for r in rows})


@dataclass(frozen=True)
class Bootstrap:
    """OP.GG lane meta (all-tier) as priors."""
    patch: str
    win_rate: Mapping[tuple[int, str], float]  # (champ, role) -> raw win rate
    pick_rate: Mapping[tuple[int, str], float]  # (champ, role) -> picks per match
    ban_rate: Mapping[int, float]  # champ -> bans per match


def load_bootstrap(s: Session, patch: str) -> Bootstrap | None:
    """OP.GG rows for `patch`, or the latest patch that has them."""
    patches = s.exec(select(BootstrapRoleStats.patch).distinct()).all()
    if not patches:
        return None
    use = patch if patch in patches else max(patches, key=_version_key)
    rows = s.exec(select(BootstrapRoleStats).where(BootstrapRoleStats.patch == use)).all()
    return Bootstrap(use, {(r.champ_id, r.role): r.wins / r.games for r in rows if r.games},
                     {(r.champ_id, r.role): r.pick_rate for r in rows}, {r.champ_id: r.ban_rate for r in rows})


def bracket_stats(s: Session, patch: str, bracket: str, boot: Bootstrap | None) -> tuple[BracketStats | None, int]:
    """One bracket's stats: our crawled games blended with the OP.GG prior. Returns (stats, crawled
    matches); stats is None when there is neither crawl data nor a bootstrap.

    win rate  = (wins + k * prior) / (games + k),   k = 50, prior = OP.GG win rate (else 50%)
    pick/ban  = (count + M * prior) / (matches + M), M = 1,000 matches
    matchups and synergy come from the crawl only, shrunk towards 50% (k = 200)."""
    total = s.get(BracketTotal, (patch, bracket))
    n = total.matches if total else 0
    if n == 0 and boot is None:
        return None, 0
    crawl = {(r.champ_id, r.role): r for r in s.exec(
        select(ChampRoleStats).where(ChampRoleStats.patch == patch, ChampRoleStats.bracket == bracket))}
    keys = set(crawl) | (set(boot.win_rate) | set(boot.pick_rate) if boot else set())

    meta: dict[int, dict[str, RoleStat]] = {}
    crawled_bans: dict[int, int] = {}
    for c, r in keys:
        row = crawl.get((c, r))
        games, wins, picks = (row.games, row.wins, row.picks) if row else (0, 0, 0)
        if row:
            crawled_bans[c] = row.bans
        prior_wr = boot.win_rate.get((c, r), 0.5) if boot else 0.5
        win_rate = (wins + ROLE_PRIOR_GAMES * prior_wr) / (games + ROLE_PRIOR_GAMES)
        if boot:
            pick_rate = (picks + RATE_PRIOR_MATCHES * boot.pick_rate.get((c, r), 0.0)) / (n + RATE_PRIOR_MATCHES)
        else:
            pick_rate = picks / n
        meta.setdefault(c, {})[r] = RoleStat(win_rate, pick_rate)

    bans = {}
    for c in meta:
        if boot:
            bans[c] = (crawled_bans.get(c, 0) + RATE_PRIOR_MATCHES * boot.ban_rate.get(c, 0.0)) / (n + RATE_PRIOR_MATCHES)
        else:
            bans[c] = crawled_bans.get(c, 0) / n
    matchups = {(m.role, m.champ_a, m.champ_b): shrink(m.wins_a, m.games, PAIR_K)
                for m in s.exec(select(Matchup).where(Matchup.patch == patch, Matchup.bracket == bracket))}
    synergy = {frozenset((y.champ_a, y.champ_b)): shrink(y.wins, y.games, PAIR_K)
               for y in s.exec(select(Synergy).where(Synergy.patch == patch, Synergy.bracket == bracket))}
    return BracketStats(meta, matchups, synergy, bans), n


def placeholder_team(data: EngineData, brackets: Mapping[str, str] = TEAM_BRACKETS) -> OurTeam:
    """Each role's four most-picked champions in that player's bracket: main, comfortable x2, learning."""
    tiers = {}
    for role in ROLES:
        meta = data.brackets[brackets[role]].meta
        ranked = sorted((c for c in meta if role in meta[c]), key=lambda c: (-meta[c][role].pick_rate, c))
        tiers[role] = dict(zip(ranked[:4], ("main", "comfortable", "comfortable", "learning")))
    return OurTeam.from_tiers(tiers, brackets)


def stats_version(db: Engine) -> tuple:
    """Changes whenever aggregation, the OP.GG bootstrap, pools or players change. Cheap to compute."""
    from sqlalchemy import text

    with db.connect() as conn:
        totals = tuple(conn.execute(text("SELECT patch, bracket, matches FROM bracket_totals ORDER BY patch, bracket")))
        boot = conn.execute(text("SELECT MAX(fetched_at), COUNT(*) FROM bootstrap_role_stats")).one()
        pools = conn.execute(text("SELECT COUNT(*), COALESCE(SUM(champ_id), 0), "
                                  "COALESCE(SUM(LENGTH(manual_tier)), 0) FROM pool_entries")).one()
        players = tuple(conn.execute(text("SELECT puuid, role, bracket FROM players ORDER BY role")))
    return totals, tuple(boot), tuple(pools), players


def load_engine_inputs(db: Engine) -> EngineInputs:
    catalog = load_catalog(db)
    names = {c: name for c, (_, name) in catalog.champs.items()}
    if not names:
        log.warning("champions table is empty; run `uv run python -m draftassist.data.ddragon`")
    patch = ".".join(catalog.patch.split(".")[:2]) if catalog.patch else None

    if patch:
        with Session(db) as s:
            boot = load_bootstrap(s, patch)
            needed = brackets_in_use(db)
            brackets: dict[str, BracketStats] = {}
            crawled: dict[str, int] = {}
            for b in needed:
                stats, crawled[b] = bracket_stats(s, patch, b, boot)
                if stats is not None:
                    brackets[b] = stats
        if len(brackets) == len(needed):
            data = EngineData(brackets, names)
            sources = {b: ("crawl" if boot is None else "opgg" if crawled[b] == 0 else "blend") for b in needed}
            kinds = set(sources.values())
            source = kinds.pop() if len(kinds) == 1 else "blend"
            games = ", ".join(f"{b} {crawled[b]:,}" for b in needed)
            if boot is None:
                note = f"Stats: our crawl only ({games} games)"
            elif not any(crawled.values()):
                note = f"Stats: OP.GG all-tier (patch {boot.patch}); crawl not started"
            else:
                note = f"Stats: OP.GG all-tier (patch {boot.patch}) blended with our crawl: {games} games"
            team = load_team(db)
            if team is None:
                team, note = placeholder_team(data), f"{note}; {PLACEHOLDER_POOL_NOTE}"
            elif all(n >= FULL_CRAWL_MATCHES for n in crawled.values()):
                note = None  # crawl dominates everywhere and pools are real: nothing to flag
            return EngineInputs(data, team, catalog, source, note, sources)

    data, team = synthetic_data(names=names or None)
    return EngineInputs(data, load_team(db) or team, catalog, "fixture", FIXTURE_NOTE)
