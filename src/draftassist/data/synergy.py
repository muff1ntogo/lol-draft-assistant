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
    """'16.21' -> (16, 21); a dataset window (ds:recent) or the selection's key sorts before every patch."""
    try:
        return tuple(int(x) for x in patch.split("."))
    except (ValueError, AttributeError):
        return (-1,)


def is_patch(window: str | None) -> bool:
    return bool(window) and _version_key(window) != (-1,)


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
    """The pooling window ending at `current`: every patch with data up to it, decayed (data/counts.crawled_patches: the
    admin's crawl or a teammate's pull). `current` counts even with no matches yet, so the older patches' weights don't
    jump when its first match arrives."""
    from draftassist.data.counts import crawled_patches
    from draftassist.data.datasets import dataset_weights, is_dataset_window

    if is_dataset_window(current):  # a published dataset's own patch scope (docs/shared-data.md, Stage 4)
        return dataset_weights(conn, current)
    return window_weights([*crawled_patches(conn), current], current, half_life)


def aggregate_synergy(conn, window: str, patches: Mapping[str, float] | None = None,
                      queues: Iterable[int] | None = None) -> None:
    """Rebuild one window of synergy_pairs and synergy_role_stats, per bracket and pooled ("all"), from the per-patch
    counts (cnt_pair, cnt_role: data/counts.py), so a teammate's pull derives the same window.
    `conn` is an open connection inside the caller's transaction; `patches` maps patch -> weight."""
    weights = patches or {window: 1.0}
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    params: dict = {"window": window}
    for i, (patch, w) in enumerate(weights.items()):
        params |= {f"p{i}": patch, f"w{i}": w}
    for table in ("synergy_pairs", "synergy_role_stats"):
        conn.execute(text(f'DELETE FROM {table} WHERE "window" = :window'), params)
        # windows older than this one: nothing reads them (Live loads the newest, the fits get theirs passed), and a
        # teammate's pull derives only the newest, so the admin keeps the same (docs/shared-data.md, Stage 2)
        older = [w for (w,) in conn.execute(text(f'SELECT DISTINCT "window" FROM {table}'))
                 if is_patch(w) and is_patch(window) and _version_key(w) < _version_key(window)]
        for w in older:
            conn.execute(text(f'DELETE FROM {table} WHERE "window" = :w'), {"w": w})

    w = f"WITH w(patch, weight) AS (VALUES {values}) "
    queue_filter = ""
    if queues is not None:  # a user's selection: only these queues (data/datasets.py)
        queue_filter = " AND c.queue IN (" + ", ".join(str(int(q)) for q in queues) + ")"
    brackets = [r[0] for r in conn.execute(text(
        f"{w}SELECT DISTINCT c.bracket FROM cnt_bracket c JOIN w ON w.patch = c.patch ORDER BY 1"), params)]
    for bracket in [*brackets, ALL]:
        where = ("WHERE 1 = 1" if bracket == ALL else "WHERE c.bracket = :bracket") + queue_filter
        p = {**params, "bracket": bracket}
        conn.execute(text(f"""{w}
            INSERT INTO synergy_role_stats ("window", bracket, champ_id, role, games, games_w, wins_w)
            SELECT :window, :bracket, c.champ_id, c.role, SUM(c.games), SUM(c.games * w.weight), SUM(c.wins * w.weight)
            FROM cnt_role c JOIN w ON w.patch = c.patch {where}
            GROUP BY c.champ_id, c.role"""), p)
        # same team, different roles: 10 pairs per team, each stored once ordered by (role, champ)
        conn.execute(text(f"""{w}
            INSERT INTO synergy_pairs ("window", bracket, role_a, champ_a, role_b, champ_b, games, games_w, wins_w)
            SELECT :window, :bracket, c.role_a, c.champ_a, c.role_b, c.champ_b, SUM(c.games), SUM(c.games * w.weight),
                   SUM(c.wins * w.weight)
            FROM cnt_pair c JOIN w ON w.patch = c.patch {where}
            GROUP BY c.role_a, c.champ_a, c.role_b, c.champ_b"""), p)


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


def latest_window(db: Engine, conn=None) -> str | None:
    """The window Live reads: a user's selected dataset window (data/datasets.apply_selection) while it's set, else the
    newest patch window."""
    from draftassist.data.datasets import active_window

    def pick(c):
        windows = [r[0] for r in c.execute(text('SELECT DISTINCT "window" FROM synergy_role_stats'))]
        active = active_window(c)
        if active is not None and active in windows:
            return active
        patches = [w for w in windows if is_patch(w)]
        return max(patches, key=_version_key) if patches else None

    if conn is not None:
        return pick(conn)
    with db.connect() as c:
        return pick(c)


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


def pick_fit(rows, window: str | None):
    """The fit row for `window` (a selected dataset's), else the newest patch window's, else any."""
    for r in rows:
        if r.window == window:
            return r
    patches = [r for r in rows if is_patch(r.window)]
    return max(patches, key=lambda r: _version_key(r.window)) if patches else rows[0]
