"""Crawled matches -> per-patch, per-bracket stats tables (spec, Data model).

Raw counts are stored; shrinkage towards 50% is applied when the engine reads them:
    wr_adj = (wins + 0.5 k) / (games + k),  k = 50 for role win rates, 200 for matchups and synergy.
Re-running is idempotent: a (patch, bracket) slice is replaced as a whole.
"""
from __future__ import annotations

import json

from sqlalchemy import Engine, text
from sqlmodel import Session

ROLE_K = 50
PAIR_K = 200
ROLE_SHARE_FOR_CHAMPION_ROLES = 0.10  # champions.roles lists roles with at least this share of games


def shrink(wins: float, games: float, k: float) -> float:
    return (wins + 0.5 * k) / (games + k)


_SLICE = "JOIN crawled_matches cm ON cm.match_id = {alias}.match_id AND cm.patch = :patch AND cm.bracket = :bracket"


def aggregate(db: Engine, patch: str) -> dict[str, int]:
    """Rebuild champ_role_stats, matchups, synergy and bracket_totals for every bracket crawled
    on `patch`; refresh champions.roles. Returns matches per bracket."""
    with Session(db) as s:
        conn = s.connection()
        brackets = [r[0] for r in conn.execute(
            text("SELECT DISTINCT bracket FROM crawled_matches WHERE patch = :patch"), {"patch": patch})]
        totals = {}
        for bracket in brackets:
            p = {"patch": patch, "bracket": bracket}
            for table in ("champ_role_stats", "matchups", "synergy", "bracket_totals"):
                conn.execute(text(f"DELETE FROM {table} WHERE patch = :patch AND bracket = :bracket"), p)

            matches = conn.execute(text(
                "SELECT COUNT(*) FROM crawled_matches WHERE patch = :patch AND bracket = :bracket"), p).scalar()
            conn.execute(text(
                "INSERT INTO bracket_totals (patch, bracket, matches) VALUES (:patch, :bracket, :matches)"),
                {**p, "matches": matches})
            totals[bracket] = matches

            conn.execute(text(f"""
                INSERT INTO champ_role_stats (patch, bracket, champ_id, role, games, wins, picks, bans)
                SELECT :patch, :bracket, mp.champ_id, mp.role, COUNT(*), SUM(mp.win), COUNT(*),
                       COALESCE((SELECT COUNT(DISTINCT mb.match_id) FROM match_bans mb {_SLICE.format(alias='mb')}
                                 WHERE mb.champ_id = mp.champ_id), 0)
                FROM match_participants mp {_SLICE.format(alias='mp')}
                GROUP BY mp.champ_id, mp.role"""), p)

            conn.execute(text(f"""
                INSERT INTO matchups (patch, bracket, role, champ_a, champ_b, games, wins_a)
                SELECT :patch, :bracket, a.role, a.champ_id, b.champ_id, COUNT(*), SUM(a.win)
                FROM match_participants a {_SLICE.format(alias='a')}
                JOIN match_participants b ON b.match_id = a.match_id AND b.role = a.role
                     AND b.team_id <> a.team_id AND a.champ_id < b.champ_id
                GROUP BY a.role, a.champ_id, b.champ_id"""), p)

            conn.execute(text(f"""
                INSERT INTO synergy (patch, bracket, champ_a, champ_b, games, wins)
                SELECT :patch, :bracket, a.champ_id, b.champ_id, COUNT(*), SUM(a.win)
                FROM match_participants a {_SLICE.format(alias='a')}
                JOIN match_participants b ON b.match_id = a.match_id AND b.team_id = a.team_id
                     AND a.champ_id < b.champ_id
                GROUP BY a.champ_id, b.champ_id"""), p)

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
    return totals

