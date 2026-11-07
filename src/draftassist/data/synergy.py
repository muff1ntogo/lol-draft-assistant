"""Role-aware duo synergy: crawled matches -> synergy_pairs + synergy_role_stats -> SynergyTable.

Synergy pools a bigger window than the meta stats, because interaction effects are fairly stable
across patches while champion strength isn't (docs/synergy-visualizer.md, A2):
- patches in the window are weighted 0.5 ** (patches_ago / 6);
- every crawled bracket is pooled into bracket "all", and each bracket is also kept on its own;
- champion strength p_A comes from the same weighted window, so it is removed consistently.

The window is every crawled patch up to the current one (`crawl_weights`), so a new patch doesn't empty what's
stable across patches. Win rates and lane matchups (champ_role_stats, matchups) stay per patch.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import Engine, text
from sqlmodel import Session, select

from draftassist.db.models import SynergyPair, SynergyRoleStats
from draftassist.engine.synergy import EMPTY, SynergyTable, pair_score, strength

ALL = "all"
DECAY_PATCHES = 6  # weight halves every 6 patches


def _version_key(patch: str) -> tuple[int, ...]:
    return tuple(int(x) for x in patch.split("."))


def window_weights(patches: Iterable[str], current: str, half_life: float = DECAY_PATCHES) -> dict[str, float]:
    """patch -> 0.5 ** (patches_ago / half_life), where patches_ago counts the distinct patches in the window
    newer than it; half_life 0 weighs every patch the same. Patches after `current` are left out."""
    ordered = sorted({p for p in patches if _version_key(p) <= _version_key(current)}, key=_version_key, reverse=True)
    return {p: 0.5 ** (i / half_life) if half_life else 1.0 for i, p in enumerate(ordered)}


def weights_cte(weights: Mapping[str, float]) -> tuple[str, dict]:
    """The VALUES list and bound parameters for `WITH w(patch, weight) AS (VALUES ...)`."""
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    params: dict = {}
    for i, (patch, w) in enumerate(weights.items()):
        params |= {f"p{i}": patch, f"w{i}": w}
    return values, params


def crawl_weights(conn, current: str, half_life: float = DECAY_PATCHES) -> dict[str, float]:
    """The pooling window ending at `current`: every crawled patch up to it, decayed. `current` counts even with no
    matches yet, so the older patches' weights don't jump when its first match arrives."""
    patches = [r[0] for r in conn.execute(text("SELECT DISTINCT patch FROM crawled_matches"))]
    return window_weights([*patches, current], current, half_life)


def aggregate_synergy(conn, window: str, patches: Mapping[str, float] | None = None) -> None:
    """Rebuild one window of synergy_pairs and synergy_role_stats, per bracket and pooled ("all").
    `conn` is an open connection inside the caller's transaction; `patches` maps patch -> weight."""
    weights = patches or {window: 1.0}
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    params: dict = {"window": window}
    for i, (patch, w) in enumerate(weights.items()):
        params |= {f"p{i}": patch, f"w{i}": w}
    for table in ("synergy_pairs", "synergy_role_stats"):
        conn.execute(text(f'DELETE FROM {table} WHERE "window" = :window'), params)

    brackets = [r[0] for r in conn.execute(text(
        f"WITH w(patch, weight) AS (VALUES {values}) "
        "SELECT DISTINCT cm.bracket FROM crawled_matches cm JOIN w ON w.patch = cm.patch"), params)]
    for bracket in [*brackets, ALL]:
        where = "" if bracket == ALL else "WHERE cm.bracket = :bracket"
        p = {**params, "bracket": bracket}
        matches = (f"WITH w(patch, weight) AS (VALUES {values}), "
                   f"m AS (SELECT cm.match_id, w.weight FROM crawled_matches cm JOIN w ON w.patch = cm.patch {where})")
        conn.execute(text(f"""{matches}
            INSERT INTO synergy_role_stats ("window", bracket, champ_id, role, games, games_w, wins_w)
            SELECT :window, :bracket, mp.champ_id, mp.role, COUNT(*), SUM(m.weight), SUM(mp.win * m.weight)
            FROM match_participants mp JOIN m ON m.match_id = mp.match_id
            GROUP BY mp.champ_id, mp.role"""), p)
        # same team, different roles: 10 pairs per team, each stored once ordered by (role, champ)
        conn.execute(text(f"""{matches}
            INSERT INTO synergy_pairs ("window", bracket, role_a, champ_a, role_b, champ_b, games, games_w, wins_w)
            SELECT :window, :bracket, a.role, a.champ_id, b.role, b.champ_id, COUNT(*), SUM(m.weight),
                   SUM(a.win * m.weight)
            FROM match_participants a JOIN m ON m.match_id = a.match_id
            JOIN match_participants b ON b.match_id = a.match_id AND b.team_id = a.team_id
                 AND (a.role < b.role OR (a.role = b.role AND a.champ_id < b.champ_id))
            GROUP BY a.role, a.champ_id, b.role, b.champ_id"""), p)


def load_damage(db: Engine, patch: str | None) -> dict[tuple[int, str], tuple[float, float, float]]:
    """Damage profile per champion and role over the window ending at `patch`, every bracket pooled (weighted by
    games and the patch's decay)."""
    if patch is None:
        return {}
    with db.connect() as conn:
        values, params = weights_cte(crawl_weights(conn, patch))
        rows = conn.execute(text(
            f"WITH w(patch, weight) AS (VALUES {values}) "
            "SELECT champ_id, role, SUM(dmg_phys * games * weight) / SUM(games * weight), "
            "SUM(dmg_magic * games * weight) / SUM(games * weight), "
            "SUM(dmg_true * games * weight) / SUM(games * weight) "
            "FROM champ_role_stats JOIN w USING (patch) "
            "WHERE dmg_phys IS NOT NULL GROUP BY champ_id, role"), params).all()
    return {(c, r): (p, m, t) for c, r, p, m, t in rows}


def latest_window(db: Engine) -> str | None:
    with Session(db) as s:
        windows = s.exec(select(SynergyRoleStats.window).distinct()).all()
    return max(windows, key=_version_key) if windows else None


def load_synergy(db: Engine, window: str | None = None, bracket: str = ALL) -> SynergyTable:
    """Score every stored pair of one window (default: the latest) and bracket."""
    window = window or latest_window(db)
    if window is None:
        return EMPTY
    with Session(db) as s:
        roles = s.exec(select(SynergyRoleStats).where(
            SynergyRoleStats.window == window, SynergyRoleStats.bracket == bracket)).all()
        rows = s.exec(select(SynergyPair).where(SynergyPair.window == window, SynergyPair.bracket == bracket)).all()
    strengths = {(r.role, r.champ_id): strength(r.wins_w, r.games_w) for r in roles}
    pairs = {}
    for r in rows:
        a, b = (r.role_a, r.champ_a), (r.role_b, r.champ_b)
        pairs[(a, b)] = pair_score(r.wins_w, r.games_w, strengths.get(a, 0.5), strengths.get(b, 0.5),
                                   raw_games=r.games)
    return SynergyTable(window, bracket, pairs, strengths)
