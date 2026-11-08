"""Crawled matches -> per-patch, per-bracket stats tables (spec, Data model).

Raw counts are stored; shrinkage towards 50% is applied when the engine reads them:
    wr_adj = (wins + 0.5 k) / (games + k),  k = 50 for role win rates, 200 for matchups.
Duo synergy is role-aware and has its own shrinkage (data/synergy.py, engine/synergy.py).
Re-running is idempotent: a (patch, bracket) slice is replaced as a whole.
"""
from __future__ import annotations

from sqlalchemy import Engine, text
from sqlmodel import Session

from draftassist.data.synergy import crawl_weights
from draftassist.data.traits import aggregate_traits

ROLE_K = 50
PAIR_K = 200


def shrink(wins: float, games: float, k: float) -> float:
    return (wins + 0.5 * k) / (games + k)


def _static():
    """Item names for the build order and item hints (data/counts.py): the Data Dragon files in the cache, if any."""
    try:
        from draftassist.config import load_settings
        from draftassist.data.builds import load_static

        return load_static(load_settings().cache_dir)
    except Exception:  # no settings or no cache: the order counts stay empty
        return None


def aggregate(db: Engine, patch: str, window: str | None = None, static=None) -> dict[str, int]:
    """The admin's aggregation of one crawled patch (docs/shared-data.md, Stage 2): its per-(tier, queue) counts from the
    per-match rows (data/counts.build_counts), then the runtime tables from the counts (counts.derive: champ_role_stats,
    matchups, bracket_totals, the synergy window ending at `window`, champions.roles), as a teammate's pull derives them.
    Then what needs per-match rows: traits, the damage thresholds and the fits. Returns matches per bracket.
    `window`: the pooled tables and fits to rebuild, when `patch` is an older patch being crawled and Live reads a newer
    window (default: `patch`). `static`: Data Dragon item names (default: from the cache)."""
    from draftassist.data.counts import build_counts, derive

    window = window or patch
    with Session(db) as s:
        conn = s.connection()
        build_counts(conn, patch, static if static is not None else _static())
        weights = crawl_weights(conn, window)  # every patch with data up to this one, decayed
        totals = derive(conn, [patch], window, weights)
        aggregate_traits(conn, window, weights)  # same window as synergy (docs/synergy-v2.md, Stage 1)
        s.commit()
    _store_damage_thresholds(db, window)
    _fit_synergy(db, window)
    _fit_matchups(db, window)
    return totals


def _store_damage_thresholds(db: Engine, window: str) -> None:
    """What a damage-heavy team is (data/builds.damage_thresholds), kept per window so Strategy and a teammate's pull read
    it without the per-match rows."""
    from draftassist.data.builds import damage_thresholds

    t = damage_thresholds(db)
    with db.begin() as conn:
        conn.execute(text('DELETE FROM damage_thresholds WHERE "window" = :w'), {"w": window})
        if t is not None:
            conn.execute(text('INSERT INTO damage_thresholds ("window", ad_heavy, ap_heavy, teams, median_ad, median_ap) '
                              "VALUES (:w, :ad, :ap, :n, :mad, :map)"),
                         {"w": window, "ad": t.ad_heavy, "ap": t.ap_heavy, "n": t.teams, "mad": t.median_ad,
                          "map": t.median_ap})


def _fit_matchups(db: Engine, patch: str) -> None:
    """Refit the matchup model (docs/matchup-archetypes.md, Stage 3). As with the synergy fits, a broken file must not
    stop the stats from refreshing."""
    import logging

    from draftassist.data.matchup_fit import fit_matchup_model

    try:
        fit_matchup_model(db, patch)
    except (OSError, ValueError, ArithmeticError):
        logging.getLogger(__name__).exception("matchup fit failed; keeping the previous fit")


def _fit_synergy(db: Engine, patch: str) -> None:
    """Refit the synergy v2 style and team rules on the new data (docs/synergy-v2.md). A broken rule file
    must not stop the stats from refreshing: it is logged and the old fits stay."""
    import logging

    from draftassist.data.synergy_model import fit_synergy_model

    try:
        fit_synergy_model(db, patch)
    except (OSError, ValueError, ArithmeticError):
        logging.getLogger(__name__).exception("synergy fit failed; keeping the previous fits")

