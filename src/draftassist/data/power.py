"""Lane power curves by milestone (docs/top-lane-power.md, Stages 4-5): a batch job, run from the Crawl page ("Lane
power") or `draftassist power`, after a crawl. Writes `power_curves`; Live's Strategy tab reads it.

1. From `match_build_events` (cheap): per (champion, role) their usual first three finished items and skill max, and per
   role the level each finished item usually comes at (the milestone axis).
2. Theory (engine/power.theory_curves) for every champion with MIN_GAMES games in a role, from Meraki's kits and Data
   Dragon's item stats. Skipped (measured only) when Meraki can't be reached and isn't cached.
3. Measured, from `match_frames`: for every lane pair and minute, the gold lead gained over the next minute, filed under
   the milestone each side had reached; per champion, the mean against the role's mean there.
4. Combined on one scale (gold per minute vs the role's average) with engine/power.combine.

Patches pool with a half-life of POWER_HALF_LIFE patches; matches crawled before frames were kept count for the item
levels and builds only.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import Engine, text
from sqlmodel import Session

from draftassist.data.builds import GameStatic, load_static, skill_max
from draftassist.data.synergy import window_weights
from draftassist.db.models import PowerCurve
from draftassist.engine import power as P

log = logging.getLogger(__name__)

POWER_HALF_LIFE = 3.0
MIN_GAMES = 30  # games in a role before a champion gets a curve
MAX_MINUTE = 25  # lane leads after this are mostly teamfights, not the lane


def _finished(purchases: list, static: GameStatic) -> list[tuple[int, int]]:
    out, seen = [], set()
    for item, t in purchases:
        it = static.items.get(item)
        if it is not None and it.finished and item not in seen:
            seen.add(item)
            out.append((item, t))
    return out


def _level_at(levels: dict[int, int], second: float) -> int:
    return max([1] + [lv for lv, t in levels.items() if t <= second])


def _weights(db: Engine, patch: str) -> dict[str, float]:
    with db.connect() as conn:
        patches = [r[0] for r in conn.execute(text("SELECT DISTINCT patch FROM crawled_matches"))]
    return window_weights([*patches, patch], patch, POWER_HALF_LIFE)


def _rows(conn, sql: str, weights: dict[str, float]):
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    params = {}
    for i, (p, w) in enumerate(weights.items()):
        params |= {f"p{i}": p, f"w{i}": w}
    return conn.execute(text(f"WITH w(patch, weight) AS (VALUES {values}) " + sql), params)


def builds_and_item_levels(db: Engine, static: GameStatic, weights: dict[str, float]):
    """((champ, role) -> (games, first three finished items, skill max)), and role -> {k: median level at item k}."""
    items: dict[tuple, Counter] = defaultdict(Counter)
    maxes: dict[tuple, Counter] = defaultdict(Counter)
    games: Counter = Counter()
    item_levels: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    with db.connect() as conn:
        for purchases, skills, levels, champ, role, w in _rows(conn, (
                "SELECT e.purchases_json, e.skills, e.levels_json, e.champ_id, p.role, w.weight FROM match_build_events e "
                "JOIN match_participants p ON p.match_id = e.match_id AND p.champ_id = e.champ_id "
                "JOIN crawled_matches m ON m.match_id = e.match_id JOIN w ON w.patch = m.patch"), weights):
            fin = _finished(json.loads(purchases or "[]"), static)
            lv = {int(k): v for k, v in json.loads(levels or "{}").items()}
            key = (champ, role)
            games[key] += 1
            if len(fin) >= 3:
                items[key][tuple(i for i, _ in fin[:3])] += w
            if (mx := skill_max(skills or "")):
                maxes[key][mx] += w
            for k, (_, t) in enumerate(fin[:3], start=1):
                item_levels[role][k].append(_level_at(lv, t))
    builds = {}
    for key, n in games.items():
        if n < MIN_GAMES or not items[key]:
            continue
        triple = items[key].most_common(1)[0][0]
        builds[key] = (n, triple, maxes[key].most_common(1)[0][0] if maxes[key] else "QEW")
    medians = {role: {k: statistics.median(v) for k, v in ks.items() if v} for role, ks in item_levels.items()}
    return builds, medians


def measure(db: Engine, static: GameStatic, weights: dict[str, float], axes: dict[str, list[P.Milestone]]):
    """(champ, role) -> per milestone [sum of gold lead gained per minute, weight]; and role -> per milestone the same."""
    from draftassist.data.timelines import read_frames

    per: dict[tuple, dict[int, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    role_tot: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    with db.connect() as conn:
        rows = _rows(conn, (
            "SELECT f.match_id, f.champ_id, f.frames_z, p.role, p.team_id, e.purchases_json, w.weight FROM match_frames f "
            "JOIN match_participants p ON p.match_id = f.match_id AND p.champ_id = f.champ_id "
            "JOIN crawled_matches m ON m.match_id = f.match_id JOIN w ON w.patch = m.patch "
            "LEFT JOIN match_build_events e ON e.match_id = f.match_id AND e.champ_id = f.champ_id "
            "ORDER BY f.match_id"), weights)
        match, players = None, []

        def flush(players):
            by_role: dict[str, list] = defaultdict(list)
            for pl in players:
                by_role[pl["role"]].append(pl)
            for role, pair in by_role.items():
                if len(pair) != 2 or pair[0]["team"] == pair[1]["team"] or role not in axes:
                    continue
                a, b = pair
                fa, fb = a["frames"], b["frames"]
                for mnt in range(1, min(len(fa), len(fb), MAX_MINUTE + 1) - 1):
                    lead_now = (fa[mnt][1] or 0) - (fb[mnt][1] or 0)
                    lead_next = (fa[mnt + 1][1] or 0) - (fb[mnt + 1][1] or 0)
                    gained = lead_next - lead_now
                    for me, sign in ((a, 1), (b, -1)):
                        lv = me["frames"][mnt][0] or 1
                        n_items = sum(1 for _, t in me["finished"] if t <= mnt * 60)
                        idx = P.milestone_index(axes[role], lv, n_items)
                        cell = per[(me["champ"], role)][idx]
                        cell[0] += sign * gained * a["w"]
                        cell[1] += a["w"]
                        tot = role_tot[role][idx]
                        tot[0] += sign * gained * a["w"]
                        tot[1] += a["w"]

        for match_id, champ, blob, role, team, purchases, w in rows:
            if match_id != match:
                flush(players)
                match, players = match_id, []
            try:
                frames = read_frames(blob)
            except Exception:
                continue
            players.append({"champ": champ, "role": role, "team": team, "frames": frames, "w": w,
                            "finished": _finished(json.loads(purchases or "[]"), static)})
        flush(players)
    return per, role_tot


def compute(db: Engine, cache_dir: Path, patch: str, kits: dict | None = None) -> dict[str, int]:
    """Rebuild `power_curves`. `kits`: Meraki kits (champ id -> kit); None fetches them (cached for a week)."""
    static = load_static(cache_dir)
    if not static.items:
        raise RuntimeError("no Data Dragon item data cached: run `draftassist ddragon` first")
    if kits is None:
        try:
            from draftassist.data.meraki import load_kits

            kits = load_kits(cache_dir)
        except Exception as e:  # measured curves still work without the theory
            log.warning("Meraki unavailable (%s): measured curves only", e)
            kits = {}
    weights = _weights(db, patch)
    builds, item_levels = builds_and_item_levels(db, static, weights)
    roles = sorted({r for _, r in builds})
    axes = {r: P.milestones(r, item_levels.get(r)) for r in roles}
    theory: dict[tuple, list[float]] = {}
    spike_at: dict[tuple, list[int]] = {}
    for role in roles:
        role_builds = {}
        for (c, r), (_, triple, mx) in builds.items():
            if r == role and c in kits:
                role_builds[c] = P.Build(tuple(static.items[i].stats for i in triple), mx)
        curves = P.theory_curves(kits, role_builds, role, item_levels.get(role))
        for c, v in curves.items():
            theory[(c, role)] = v
        for c, idx in P.spikes(curves).items():
            spike_at[(c, role)] = idx
    per, role_tot = measure(db, static, weights, axes)
    measured: dict[tuple, list[tuple[float | None, float]]] = {}
    for key in builds:
        c, role = key
        axis = axes[role]
        cells = []
        for i in range(len(axis)):
            s, n = per.get(key, {}).get(i, (0.0, 0.0))
            ts, tn = role_tot[role].get(i, (0.0, 0.0))
            cells.append(((s / n - ts / tn) if n > 0 and tn > 0 else None, n))
        measured[key] = cells
    slope = P.fit_slope((theory[k][i], m, n) for k, cells in measured.items() if k in theory
                        for i, (m, n) in enumerate(cells) if m is not None)
    written = 0
    with Session(db) as s:
        s.exec(text("DELETE FROM power_curves"))
        for key, (games, _, _) in builds.items():
            c, role = key
            for i, m in enumerate(axes[role]):
                th = theory.get(key, [None] * len(axes[role]))[i]
                ms, n = measured[key][i]
                s.add(PowerCurve(champ_id=c, role=role, idx=i, label=m.label, level=m.level, items=m.items,
                                 theory=None if th is None else round(th, 4),
                                 measured=None if ms is None else round(ms, 2), n=round(n, 1),
                                 combined=None if (cb := P.combine(th, ms, n, slope)) is None else round(cb, 2),
                                 spike=i in spike_at.get(key, []), window=patch, games=games))
                written += 1
        s.commit()
    return {"champion_roles": len(builds), "rows": written, "with_theory": len(theory), "slope": round(slope, 1)}


def main() -> int:
    from draftassist.config import load_settings
    from draftassist.data.crawler import current_patch
    from draftassist.db.session import init_db

    settings = load_settings()
    db = init_db(settings.db_path)
    result = compute(db, settings.cache_dir, current_patch(db))
    print(f"Lane power: {result['champion_roles']} champion-roles, {result['with_theory']} with theory, "
          f"{result['rows']} rows; theory scale {result['slope']} gold/min per unit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
