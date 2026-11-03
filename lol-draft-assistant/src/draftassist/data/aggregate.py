"""Crawled matches -> per-patch, per-bracket stats tables (spec, Data model).

Raw counts are stored; shrinkage towards 50% is applied when the engine reads them:
    wr_adj = (wins + 0.5 k) / (games + k),  k = 50 for role win rates, 200 for matchups.
Duo synergy is role-aware and has its own shrinkage (data/synergy.py, engine/synergy.py).
Re-running is idempotent: a (patch, bracket) slice is replaced as a whole.
"""
from __future__ import annotations

import json

from sqlalchemy import Engine, text
from sqlmodel import Session

from draftassist.data.synergy import aggregate_synergy
from draftassist.data.traits import aggregate_traits
from draftassist.engine.comp import LONG_S, SHORT_S

ROLE_K = 50
PAIR_K = 200
ROLE_SHARE_FOR_CHAMPION_ROLES = 0.10  # champions.roles lists roles with at least this share of games


def shrink(wins: float, games: float, k: float) -> float:
    return (wins + 0.5 * k) / (games + k)


# Mean per-game share of one damage type; games without damage data (NULL) are left out of the mean.
_DAMAGE_SHARE = "AVG(CAST(mp.{col} AS REAL) / NULLIF(mp.dmg_phys + mp.dmg_magic + mp.dmg_true, 0))"
_SLICE = "JOIN crawled_matches cm ON cm.match_id = {alias}.match_id AND cm.patch = :patch AND cm.bracket = :bracket"


def aggregate(db: Engine, patch: str) -> dict[str, int]:
    """Rebuild champ_role_stats (with damage profile), matchups and bracket_totals for every
    bracket crawled on `patch`, and the role-aware synergy window ending at `patch` (data/synergy.py);
    refresh champions.roles. Returns matches per bracket."""
    with Session(db) as s:
        conn = s.connection()
        brackets = [r[0] for r in conn.execute(
            text("SELECT DISTINCT bracket FROM crawled_matches WHERE patch = :patch"), {"patch": patch})]
        totals = {}
        for bracket in brackets:
            p = {"patch": patch, "bracket": bracket}
            for table in ("champ_role_stats", "matchups", "bracket_totals"):
                conn.execute(text(f"DELETE FROM {table} WHERE patch = :patch AND bracket = :bracket"), p)

            matches = conn.execute(text(
                "SELECT COUNT(*) FROM crawled_matches WHERE patch = :patch AND bracket = :bracket"), p).scalar()
            conn.execute(text(
                "INSERT INTO bracket_totals (patch, bracket, matches) VALUES (:patch, :bracket, :matches)"),
                {**p, "matches": matches})
            totals[bracket] = matches

            conn.execute(text(f"""
                INSERT INTO champ_role_stats (patch, bracket, champ_id, role, games, wins, picks, bans,
                                              dmg_phys, dmg_magic, dmg_true,
                                              games_short, wins_short, games_long, wins_long)
                SELECT :patch, :bracket, mp.champ_id, mp.role, COUNT(*), SUM(mp.win), COUNT(*),
                       COALESCE((SELECT COUNT(DISTINCT mb.match_id) FROM match_bans mb {_SLICE.format(alias='mb')}
                                 WHERE mb.champ_id = mp.champ_id), 0),
                       {_DAMAGE_SHARE.format(col='dmg_phys')}, {_DAMAGE_SHARE.format(col='dmg_magic')},
                       {_DAMAGE_SHARE.format(col='dmg_true')},
                       SUM(cm.game_duration_s < :short_s), SUM(mp.win * (cm.game_duration_s < :short_s)),
                       SUM(cm.game_duration_s > :long_s), SUM(mp.win * (cm.game_duration_s > :long_s))
                FROM match_participants mp {_SLICE.format(alias='mp')}
                GROUP BY mp.champ_id, mp.role"""), {**p, "short_s": SHORT_S, "long_s": LONG_S})

            conn.execute(text(f"""
                INSERT INTO matchups (patch, bracket, role, champ_a, champ_b, games, wins_a)
                SELECT :patch, :bracket, a.role, a.champ_id, b.champ_id, COUNT(*), SUM(a.win)
                FROM match_participants a {_SLICE.format(alias='a')}
                JOIN match_participants b ON b.match_id = a.match_id AND b.role = a.role
                     AND b.team_id <> a.team_id AND a.champ_id < b.champ_id
                GROUP BY a.role, a.champ_id, b.champ_id"""), p)

        aggregate_synergy(conn, patch)
        aggregate_traits(conn, patch)  # same window as synergy (docs/synergy-v2.md, Stage 1)

        # champions.roles: roles with a meaningful share of the champion's games, all brackets pooled
        rows = conn.execute(text(
            "SELECT champ_id, role, SUM(games) FROM champ_role_stats WHERE patch = :patch GROUP BY champ_id, role"),
            {"patch": patch}).all()
        by_champ: dict[int, dict[str, int]] = {}
        for champ, role, games in rows:
            by_champ.setdefault(champ, {})[role] = games
        for champ, roles in by_champ.items():
            total = sum(roles.values())
            keep = sorted((r for r, g in roles.items() if g / total >= ROLE_SHARE_FOR_CHAMPION_ROLES),
                          key=lambda r: -roles[r])
            conn.execute(text("UPDATE champions SET roles = :roles WHERE champ_id = :champ"),
                         {"roles": json.dumps(keep), "champ": champ})
        s.commit()
    _fit_synergy(db, patch)
    return totals


def _fit_synergy(db: Engine, patch: str) -> None:
    """Refit the synergy v2 style and team rules on the new data (docs/synergy-v2.md). A broken rule file
    must not stop the stats from refreshing: it is logged and the old fits stay."""
    import logging

    from draftassist.data.synergy_model import fit_synergy_model

    try:
        fit_synergy_model(db, patch)
    except (OSError, ValueError, ArithmeticError):
        logging.getLogger(__name__).exception("synergy fit failed; keeping the previous fits")

