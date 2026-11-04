"""Measured champion traits: crawled matches -> champion_traits -> ChampionTraits (docs/synergy-v2.md, Stage 1).

Same pooled, time-decayed window as synergy_pairs (data/synergy.py): patches weighted
0.5 ** (patches_ago / 6), every bracket on its own and pooled as "all". Per champion and role:
- per-minute rates and weighted means of the source columns (data/traits_map.py), games with a NULL
  source left out of that mean;
- each source turned into a percentile within the role (champion-roles with >= TRAIT_MIN_GAMES games);
  a trait with several sources is the mean of their percentiles;
- the power curve slope over three game-length buckets, and its percentile;
- damage shares as real shares, and ranged from Data Dragon.
"""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Engine, text
from sqlmodel import Session, col, select

from draftassist.data.synergy import ALL, latest_window
from draftassist.data.traits_map import TRAIT_SOURCES
from draftassist.db.models import Champion, ChampionTraitRow, MatchupFit
from draftassist.engine.comp import LONG_S, SHORT_S
from draftassist.engine.traits import ChampionTraits, KitTraits, percentiles, power_curve_slope, ranged

TRAIT_MIN_GAMES = 20  # fewer raw games in the window: no percentile traits (too noisy)

_SOURCES = sorted({src for sources in TRAIT_SOURCES.values() for src in sources})
_DMG = "CAST(mp.{col} AS REAL) / NULLIF(mp.dmg_phys + mp.dmg_magic + mp.dmg_true, 0)"


def _expr(col: str, per_minute: bool) -> str:
    return f"(mp.{col} * 60.0 / NULLIF(m.dur, 0))" if per_minute else f"mp.{col}"


def _wmean(expr: str) -> str:
    return f"SUM(m.weight * {expr}) / NULLIF(SUM(m.weight * ({expr} IS NOT NULL)), 0)"


def _select() -> str:
    cols = [_wmean(_expr(c, pm)) for c, pm in _SOURCES]
    cols += [_wmean(_DMG.format(col=c)) for c in ("dmg_phys", "dmg_magic", "dmg_true")]
    short, long = "(m.dur < :short_s)", "(m.dur > :long_s)"
    mid = "(m.dur >= :short_s AND m.dur <= :long_s)"
    for b in (short, mid, long):
        cols += [f"SUM(m.weight * {b})", f"SUM(m.weight * mp.win * {b})"]
    return ", ".join(cols)


def aggregate_traits(conn, window: str, patches: Mapping[str, float] | None = None) -> None:
    """Rebuild one window of champion_traits, per bracket and pooled ("all"). `conn` is an open
    connection inside the caller's transaction; `patches` maps patch -> weight (default: `window` only)."""
    weights = patches or {window: 1.0}
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    params: dict = {"window": window, "short_s": SHORT_S, "long_s": LONG_S}
    for i, (patch, w) in enumerate(weights.items()):
        params |= {f"p{i}": patch, f"w{i}": w}
    conn.execute(text('DELETE FROM champion_traits WHERE "window" = :window'), params)
    ranges = dict(conn.execute(text("SELECT champ_id, attack_range FROM champions")).all())
    brackets = [r[0] for r in conn.execute(text(
        f"WITH w(patch, weight) AS (VALUES {values}) "
        "SELECT DISTINCT cm.bracket FROM crawled_matches cm JOIN w ON w.patch = cm.patch"), params)]
    for bracket in [*brackets, ALL]:
        where = "" if bracket == ALL else "WHERE cm.bracket = :bracket"
        rows = conn.execute(text(f"""
            WITH w(patch, weight) AS (VALUES {values}),
            m AS (SELECT cm.match_id, w.weight, cm.game_duration_s AS dur
                  FROM crawled_matches cm JOIN w ON w.patch = cm.patch {where})
            SELECT mp.champ_id, mp.role, COUNT(*), SUM(m.weight), {_select()}
            FROM match_participants mp JOIN m ON m.match_id = mp.match_id
            GROUP BY mp.champ_id, mp.role"""), {**params, "bracket": bracket}).all()
        for row in _trait_rows(rows, ranges):
            conn.execute(text(
                'INSERT INTO champion_traits ("window", bracket, champ_id, role, games, games_w, cc, frontline, '
                "peel, dmg_phys, dmg_magic, dmg_true, power_curve, power_curve_slope, lane_pressure, ranged, cc_all, "
                "frontline_all, peel_all) "
                "VALUES (:window, :bracket, :champ_id, :role, :games, :games_w, :cc, :frontline, :peel, "
                ":dmg_phys, :dmg_magic, :dmg_true, :power_curve, :power_curve_slope, :lane_pressure, :ranged, "
                ":cc_all, :frontline_all, :peel_all)"),
                {**row, "window": window, "bracket": bracket})


def _trait_rows(rows, ranges: Mapping[int, float | None]) -> list[dict]:
    n_src = len(_SOURCES)
    out: list[dict] = []
    raw: dict[tuple[int, str], dict] = {}
    for r in rows:
        champ, role, games, games_w = r[0], r[1], r[2], r[3]
        src = dict(zip(_SOURCES, r[4:4 + n_src]))
        dmg = r[4 + n_src:7 + n_src]
        b = r[7 + n_src:]
        buckets = [(b[1] or 0.0, b[0] or 0.0), (b[3] or 0.0, b[2] or 0.0), (b[5] or 0.0, b[4] or 0.0)]
        enough = games >= TRAIT_MIN_GAMES
        raw[(champ, role)] = {"src": src if enough else {}, "slope": power_curve_slope(buckets) if enough else None}
        out.append({"champ_id": champ, "role": role, "games": games, "games_w": games_w,
                    "dmg_phys": dmg[0], "dmg_magic": dmg[1], "dmg_true": dmg[2],
                    "power_curve_slope": raw[(champ, role)]["slope"], "ranged": ranged(ranges.get(champ))})
    for role in {k[1] for k in raw}:
        keys = [k for k in raw if k[1] == role]
        pct = {src: percentiles({k: raw[k]["src"].get(src) for k in keys}) for src in _SOURCES}
        curve = percentiles({k: raw[k]["slope"] for k in keys})
        for row in (o for o in out if o["role"] == role):
            k = (row["champ_id"], role)
            for trait, sources in TRAIT_SOURCES.items():
                vals = [pct[src][k] for src in sources if pct[src].get(k) is not None]
                row[trait] = sum(vals) / len(vals) if vals else None
            row["power_curve"] = curve.get(k)
    # the same traits ranked across every role, for team rules
    keys = list(raw)
    pct_all = {src: percentiles({k: raw[k]["src"].get(src) for k in keys}) for src in _SOURCES}
    for row in out:
        k = (row["champ_id"], row["role"])
        for trait in ("cc", "frontline", "peel"):
            vals = [pct_all[src][k] for src in TRAIT_SOURCES[trait] if pct_all[src].get(k) is not None]
            row[f"{trait}_all"] = sum(vals) / len(vals) if vals else None
    return out


def load_traits(db: Engine, window: str | None = None, bracket: str = ALL) -> ChampionTraits:
    """Measured traits for one window (default: the latest) and bracket, the tags, and attack ranges."""
    from draftassist.data.tags import load_kit, load_tags

    window = window or latest_window(db)
    with Session(db) as s:
        rows = s.exec(select(ChampionTraitRow).where(ChampionTraitRow.window == window,
                                                     ChampionTraitRow.bracket == bracket)).all() if window else []
        ranges = {c.champ_id: c.attack_range for c in s.exec(select(Champion)).all()}
    fields = ("cc", "frontline", "peel", "dmg_phys", "dmg_magic", "dmg_true", "power_curve", "power_curve_slope",
              "lane_pressure", "ranged", "cc_all", "frontline_all", "peel_all")
    measured = {(r.champ_id, r.role): {f: getattr(r, f) for f in fields} | {"games": r.games} for r in rows}
    with Session(db) as s:  # win rate when 300+ gold behind at 15 minutes (data/matchup_model.py)
        behind = s.exec(select(MatchupFit).where(MatchupFit.window == window, col(MatchupFit.param).startswith("behind:"))
                        ).all() if window else []
    for r in behind:
        key = (int(r.param.split(":")[1]), r.role)
        if key in measured:
            measured[key]["behind_value"] = r.value
    return ChampionTraits(measured, load_tags(db), ranges, KitTraits(load_kit(db)))
