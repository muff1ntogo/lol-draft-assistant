"""Builds the engine's inputs from the database: champion catalog (names, icons), stats, our team.

Stats per bracket blend our crawl with the OP.GG bootstrap (all-tier): OP.GG is the prior each
champion starts from, and the bracket's own games take over as they accumulate (bracket_stats).
Matchups come from the crawl, shrunk towards what strength, archetypes and kits predict once a model is fitted
(data/matchup_model.py); so does role-aware synergy (data/synergy.py, pooled over every bracket by default) and the
damage profile. If neither source exists, everything falls back to
synthetic data over the real champion ids.

Our team comes from imported pools (data/pools.py); before any import, each role gets placeholder
pools (its most-picked champions).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.aggregate import PAIR_K, shrink
from draftassist.data.crawler import brackets_in_use, display_patch
from draftassist.data.ddragon import BASE_URL
from draftassist.data.matchup_model import (
    STRENGTH_K,
    load_matchup_model,
    smoothed_matchups,
)
from draftassist.data.pools import load_team
from draftassist.data.profiles import load_profiles
from draftassist.data.traits import load_traits
from draftassist.db.models import (
    BootstrapRoleStats,
    BracketTotal,
    Champion,
    ChampRoleStats,
    Matchup,
)
from draftassist.engine.mathx import logit
from draftassist.engine.model import ROLES, BracketStats, EngineData, OurTeam, RoleStat
from draftassist.engine.simulate import TEAM_BRACKETS, synthetic_data

log = logging.getLogger(__name__)

# OP.GG's win rate weighs as much as this many of the bracket's own games. Was ROLE_K (50); 500 since 14 Nov 2026:
# the bracket's own rates vary beyond OP.GG's by only 1-2.5pp, and on 1,203 held-out games (data/draft_eval.py) 500
# scored a Brier 0.0033 better (95% interval 0.0006..0.0061) and AUC 0.550 vs 0.539. 1,000 scored the same as 500.
ROLE_PRIOR_GAMES = 500
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


def strength_win_rate(wins: float, games: float, prior: float) -> float:
    """A champion's strength in the matchup prior: towards OP.GG's rate (else 50%) with far more weight than the
    engine's role win rate gives it (data/matchup_model.py, STRENGTH_K)."""
    return (wins + STRENGTH_K * prior) / (games + STRENGTH_K)


def bracket_stats(s: Session, patch: str, bracket: str, boot: Bootstrap | None,
                  model=None, lane_slopes: Mapping | None = None,
                  role_prior_games: float = ROLE_PRIOR_GAMES) -> tuple[BracketStats | None, int]:
    """One bracket's stats: our crawled games blended with the OP.GG prior. Returns (stats, crawled
    matches); stats is None when there is neither crawl data nor a bootstrap.

    win rate  = (wins + k * prior) / (games + k),   k = 50, prior = OP.GG win rate (else 50%)
    pick/ban  = (count + M * prior) / (matches + M), M = 1,000 matches
    matchups come from the crawl; with a fitted `model` they shrink towards its prediction (k_r per role), without
    one towards 50% (k = 200)."""
    total = s.get(BracketTotal, (patch, bracket))
    n = total.matches if total else 0
    if n == 0 and boot is None:
        return None, 0
    crawl = {(r.champ_id, r.role): r for r in s.exec(
        select(ChampRoleStats).where(ChampRoleStats.patch == patch, ChampRoleStats.bracket == bracket))}
    keys = set(crawl) | (set(boot.win_rate) | set(boot.pick_rate) if boot else set())

    meta: dict[int, dict[str, RoleStat]] = {}
    strength: dict[tuple[int, str], float] = {}
    crawled_bans: dict[int, int] = {}
    for c, r in keys:
        row = crawl.get((c, r))
        games, wins, picks = (row.games, row.wins, row.picks) if row else (0, 0, 0)
        if row:
            crawled_bans[c] = row.bans
        prior_wr = boot.win_rate.get((c, r), 0.5) if boot else 0.5
        win_rate = (wins + role_prior_games * prior_wr) / (games + role_prior_games)
        strength[(c, r)] = logit(strength_win_rate(wins, games, prior_wr))
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
    rows = s.exec(select(Matchup).where(Matchup.patch == patch, Matchup.bracket == bracket)).all()
    if model is not None:
        lane_slopes = lane_slopes if lane_slopes is not None else model.lane_slopes()
        matchups, games = smoothed_matchups(model, meta, rows, strength)
        return BracketStats(meta, matchups, bans, True, games, {r: model.k(r) for r in ROLES}, strength,
                            model.lane_table(), lane_slopes), n
    matchups = {(m.role, m.champ_a, m.champ_b): shrink(m.wins_a, m.games, PAIR_K) for m in rows}
    return BracketStats(meta, matchups, bans), n


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
        pools = conn.execute(text("SELECT COUNT(*), COALESCE(SUM(champ_id), 0), COALESCE(SUM(LENGTH(manual_tier)), 0), "
                                  "COALESCE(SUM(games), 0), COALESCE(SUM(wins), 0), COALESCE(SUM(tier_locked), 0) "
                                  "FROM pool_entries")).one()
        players = tuple(conn.execute(text("SELECT puuid, role, bracket, active FROM players ORDER BY role, puuid")))
    return totals, tuple(boot), tuple(pools), players


def load_engine_inputs(db: Engine, use_matchup_model: bool = True, stats_patch: str | None = None,
                       role_prior_games: float = ROLE_PRIOR_GAMES, pair_k: float | None = None) -> EngineInputs:
    """`use_matchup_model=False`: the old 50% shrinkage, to compare (scripts/ban_audit.py). `stats_patch` and
    `role_prior_games`, `pair_k`: another patch's stats, another win rate or pair synergy shrinkage, to compare (data/draft_eval.py)."""
    catalog = load_catalog(db)
    names = {c: name for c, (_, name) in catalog.champs.items()}
    if not names:
        log.warning("champions table is empty; run `uv run python -m draftassist.data.ddragon`")
    patch = ".".join(catalog.patch.split(".")[:2]) if catalog.patch else None

    if patch:
        from draftassist.data.datasets import SEL, active_selection

        stats_key = stats_patch or (SEL if active_selection(db) else patch)  # a user's data selection (docs/shared-data.md, Stage 4)
        model = load_matchup_model(db) if use_matchup_model else None
        lane_slopes = model.lane_slopes() if model is not None else None
        with Session(db) as s:
            boot = load_bootstrap(s, patch)
            needed = brackets_in_use(db)
            brackets: dict[str, BracketStats] = {}
            crawled: dict[str, int] = {}
            for b in needed:
                stats, crawled[b] = bracket_stats(s, stats_key, b, boot, model, lane_slopes, role_prior_games)
                if stats is not None:
                    brackets[b] = stats
        if len(brackets) == len(needed):
            from draftassist.data.settings_store import get_synergy_bracket
            from draftassist.data.synergy import load_damage, load_synergy

            synergy = load_synergy(db, bracket=get_synergy_bracket(db), k=pair_k)
            data = EngineData(brackets, names, synergy=synergy, damage=load_damage(db, synergy.window or patch),
                              profiles=load_profiles(db, stats_key), bot=model.bot() if model is not None else None,
                              traits=load_traits(db) if model is not None else None,
                              labels=model.labels_table() if model is not None else None, explainer=model)
            sources = {b: ("crawl" if boot is None else "opgg" if crawled[b] == 0 else "blend") for b in needed}
            kinds = set(sources.values())
            source = kinds.pop() if len(kinds) == 1 else "blend"
            crawled = {b: round(n) for b, n in crawled.items()}  # a selection's weighted totals
            games = ", ".join(f"{b} {crawled[b]:,}" for b in needed)
            if boot is None:
                note = f"Stats: our crawl only ({games} games)"
            elif not any(crawled.values()):
                note = f"Stats: OP.GG all-tier (patch {display_patch(boot.patch)}); crawl not started"
            else:
                note = f"Stats: OP.GG all-tier (patch {display_patch(boot.patch)}) blended with our crawl: {games} games"
            team = load_team(db)
            if team is None:
                team, note = placeholder_team(data), f"{note}; {PLACEHOLDER_POOL_NOTE}"
            elif all(n >= FULL_CRAWL_MATCHES for n in crawled.values()):
                note = None  # crawl dominates everywhere and pools are real: nothing to flag
            return EngineInputs(data, team, catalog, source, note, sources)

    data, team = synthetic_data(names=names or None)
    data = replace(data, profiles=load_profiles(db, patch))
    return EngineInputs(data, load_team(db) or team, catalog, "fixture", FIXTURE_NOTE)
