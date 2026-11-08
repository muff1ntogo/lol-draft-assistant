"""Per-(patch, tier, queue) count tables (docs/shared-data.md, Stage 2) and the runtime tables derived from them.

    build_counts(conn, patch, static)    the admin: per-match rows -> cnt_* for one patch (every tier and queue)
    derive(conn, patches, window)        everyone: cnt_* -> champ_role_stats, matchups, bracket_totals for `patches`,
                                         the synergy window ending at `window`, champions.roles

The admin's aggregation and a teammate's pull run the same `derive` on the same counts, so their runtime tables are the
same. Counts are additive: a selection of patches, tiers and queues is a sum (Stage 4).
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping

from sqlalchemy import Engine, text

COUNT_TABLES = ("cnt_role", "cnt_ban", "cnt_matchup", "cnt_bracket", "cnt_pair", "cnt_build", "cnt_order", "cnt_lane")
TIME_BIN_S = 10  # first item / level 6 seconds are counted in bins this wide (for the medians)
ROLE_SHARE_FOR_CHAMPION_ROLES = 0.10  # champions.roles lists roles with at least this share of games


def version_key(patch: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in patch.split("."))
    except ValueError:
        return (0,)


def crawled_patches(conn) -> list[str]:
    """Every patch with data, oldest first: from bracket_totals (what a teammate pulled) and crawled_matches (the admin's)."""
    rows = conn.execute(text("SELECT patch FROM bracket_totals UNION SELECT patch FROM crawled_matches")).all()
    return sorted({r[0] for r in rows if r[0] and version_key(r[0]) != (0,)}, key=version_key)  # not "sel"


# ---------- the admin: per-match rows -> counts ----------
_SLICE = "JOIN crawled_matches cm ON cm.match_id = {a}.match_id AND cm.patch = :patch"
_OPP = ("JOIN match_participants o ON o.match_id = p.match_id AND o.role = p.role AND o.team_id <> p.team_id")


def build_counts(conn, patch: str, static=None) -> None:
    """Replace every count row of `patch`. `static` (data/builds.GameStatic) tells finished items and boots apart, for the
    build order and the item hints; without item names those are left empty."""
    from draftassist.engine.comp import LONG_S, SHORT_S

    p = {"patch": patch}
    for t in COUNT_TABLES:
        conn.execute(text(f"DELETE FROM {t} WHERE patch = :patch"), p)
    conn.execute(text("""
        INSERT INTO cnt_bracket (patch, bracket, queue, matches)
        SELECT patch, bracket, queue_id, COUNT(*) FROM crawled_matches WHERE patch = :patch GROUP BY bracket, queue_id"""), p)
    share = "CAST(mp.{c} AS REAL) / NULLIF(mp.dmg_phys + mp.dmg_magic + mp.dmg_true, 0)"
    conn.execute(text(f"""
        INSERT INTO cnt_role (patch, bracket, queue, champ_id, role, games, wins, dmg_phys_sum, dmg_magic_sum,
                              dmg_true_sum, dmg_n, games_short, wins_short, games_long, wins_long)
        SELECT :patch, cm.bracket, cm.queue_id, mp.champ_id, mp.role, COUNT(*), SUM(mp.win),
               COALESCE(SUM({share.format(c='dmg_phys')}), 0), COALESCE(SUM({share.format(c='dmg_magic')}), 0),
               COALESCE(SUM({share.format(c='dmg_true')}), 0), COUNT({share.format(c='dmg_phys')}),
               SUM(cm.game_duration_s < :short_s), SUM(mp.win * (cm.game_duration_s < :short_s)),
               SUM(cm.game_duration_s > :long_s), SUM(mp.win * (cm.game_duration_s > :long_s))
        FROM match_participants mp {_SLICE.format(a='mp')}
        GROUP BY cm.bracket, cm.queue_id, mp.champ_id, mp.role"""), {**p, "short_s": SHORT_S, "long_s": LONG_S})
    conn.execute(text(f"""
        INSERT INTO cnt_ban (patch, bracket, queue, champ_id, bans)
        SELECT :patch, cm.bracket, cm.queue_id, mb.champ_id, COUNT(DISTINCT mb.match_id)
        FROM match_bans mb {_SLICE.format(a='mb')} GROUP BY cm.bracket, cm.queue_id, mb.champ_id"""), p)
    conn.execute(text(f"""
        INSERT INTO cnt_matchup (patch, bracket, queue, role, champ_a, champ_b, games, wins_a, lane_gold_sum, lane_xp_sum,
                                 lane_n)
        SELECT :patch, cm.bracket, cm.queue_id, a.role, a.champ_id, b.champ_id, COUNT(*), SUM(a.win),
               COALESCE(SUM(a.lane_gold_15), 0), COALESCE(SUM(a.lane_xp_15), 0), COUNT(a.lane_gold_15)
        FROM match_participants a {_SLICE.format(a='a')}
        JOIN match_participants b ON b.match_id = a.match_id AND b.role = a.role AND b.team_id <> a.team_id
             AND a.champ_id < b.champ_id
        GROUP BY cm.bracket, cm.queue_id, a.role, a.champ_id, b.champ_id"""), p)
    conn.execute(text(f"""
        INSERT INTO cnt_pair (patch, bracket, queue, role_a, champ_a, role_b, champ_b, games, wins)
        SELECT :patch, cm.bracket, cm.queue_id, a.role, a.champ_id, b.role, b.champ_id, COUNT(*), SUM(a.win)
        FROM match_participants a {_SLICE.format(a='a')}
        JOIN match_participants b ON b.match_id = a.match_id AND b.team_id = a.team_id
             AND (a.role < b.role OR (a.role = b.role AND a.champ_id < b.champ_id))
        GROUP BY cm.bracket, cm.queue_id, a.role, a.champ_id, b.role, b.champ_id"""), p)
    _build_counts(conn, p)
    _lane_rune_counts(conn, p)
    if static is not None and static.items:
        _order_counts(conn, patch, static)


def _build_counts(conn, p) -> None:
    base = (f"FROM match_builds b JOIN match_participants p ON p.match_id = b.match_id AND p.champ_id = b.champ_id "
            f"{_SLICE.format(a='b')}")
    group = "GROUP BY cm.bracket, cm.queue_id, b.champ_id, p.role"
    insert = "INSERT INTO cnt_build (patch, bracket, queue, champ_id, role, kind, value, games, wins) "
    head = "SELECT :patch, cm.bracket, cm.queue_id, b.champ_id, p.role"
    conn.execute(text(f"{insert}{head}, 'total', '', COUNT(*), SUM(p.win) {base} {group}"), p)
    conn.execute(text(f"{insert}{head}, 'keystone', CAST(b.keystone AS TEXT), COUNT(*), SUM(p.win) {base} "
                      f"WHERE b.keystone IS NOT NULL {group}, b.keystone"), p)
    for kind, col in (("page", "perks_json"), ("spells", "spells_json")):
        conn.execute(text(f"{insert}{head}, '{kind}', b.{col}, COUNT(*), SUM(p.win) {base} "
                          f"WHERE b.{col} IS NOT NULL AND b.{col} <> '[]' {group}, b.{col}"), p)
    conn.execute(text(f"""{insert}
        SELECT :patch, bracket, queue_id, champ_id, role, 'item', CAST(item AS TEXT), COUNT(*), SUM(win) FROM (
            SELECT DISTINCT cm.bracket, cm.queue_id, b.champ_id, p.role, p.win, b.match_id, j.value AS item
            {base}, json_each(b.items_json) j)
        GROUP BY bracket, queue_id, champ_id, role, item"""), p)


def _lane_rune_counts(conn, p) -> None:
    base = (f"FROM match_builds b JOIN match_participants p ON p.match_id = b.match_id AND p.champ_id = b.champ_id "
            f"{_SLICE.format(a='b')} {_OPP}")
    gold = "COUNT(*), SUM(p.win), COUNT(p.lane_gold_15), COALESCE(SUM(p.lane_gold_15), 0), " \
           "COALESCE(SUM(p.lane_gold_15 * p.lane_gold_15), 0)"
    insert = ("INSERT INTO cnt_lane (patch, bracket, queue, champ_id, role, opp_id, kind, value, games, wins, gold_n, "
              "gold_sum, gold_sq) ")
    head = "SELECT :patch, cm.bracket, cm.queue_id, b.champ_id, p.role, o.champ_id"
    group = "GROUP BY cm.bracket, cm.queue_id, b.champ_id, p.role, o.champ_id"
    conn.execute(text(f"{insert}{head}, 'rune_total', '', {gold} {base} {group}"), p)
    conn.execute(text(f"{insert}{head}, 'rune', CAST(j.value AS TEXT), {gold} {base}, json_each(b.perks_json) j "
                      f"{group}, j.value"), p)


def _bin(seconds) -> str | None:
    return None if seconds is None else str(int(seconds) // TIME_BIN_S * TIME_BIN_S)


def _order_counts(conn, patch: str, static) -> None:
    from draftassist.data.builds import _features

    rows = conn.execute(text(f"""
        SELECT cm.bracket, cm.queue_id, e.champ_id, p.role, p.win, e.purchases_json, e.skills, e.levels_json,
               p.lane_gold_15, o.champ_id, e.match_id
        FROM match_build_events e JOIN match_participants p ON p.match_id = e.match_id AND p.champ_id = e.champ_id
        {_SLICE.format(a='e')}
        LEFT JOIN match_participants o ON o.match_id = p.match_id AND o.role = p.role AND o.team_id <> p.team_id"""),
        {"patch": patch}).all()
    order: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    lane: dict[tuple, list[float]] = defaultdict(lambda: [0, 0, 0, 0.0, 0.0])
    seen_players: set[tuple] = set()
    for bracket, queue, champ, role, win, purchases, skills, levels, gold, opp, match_id in rows:
        f = _features((purchases, skills, levels), static)
        key = (bracket, queue, champ, role)
        player = (match_id, champ)
        if player not in seen_players:  # one player, two rows when a lane had two opponents: counted once here
            seen_players.add(player)
            values = [("total", "")]
            if f["core"] is not None:
                values += [("core", json.dumps(list(f["core"]))), ("core_set", json.dumps(sorted(f["core"])))]
            if f["boots"] is not None:
                values.append(("boots", str(f["boots"])))
            if f["starter"] is not None:
                values.append(("starter", json.dumps(list(f["starter"]))))
            for k in ("first3", "max"):
                if f[k] is not None:
                    values.append((k, f[k]))
            for k in ("first_item_s", "level6_s"):
                if _bin(f[k]) is not None:
                    values.append((k, _bin(f[k])))
            for kind, value in values:
                c = order[(*key, kind, value)]
                c[0] += 1
                c[1] += int(bool(win))
        if opp is None:
            continue
        options = [("item_total", "")]
        options += [("start", str(i)) for i in sorted(set(f["starter"] or ()))]
        first = f["core"][0] if f["core"] else _first_finished(purchases, static)
        if first is not None:
            options.append(("first", str(first)))
        for kind, value in options:
            c = lane[(*key, opp, kind, value)]
            c[0] += 1
            c[1] += int(bool(win))
            if gold is not None:
                c[2] += 1
                c[3] += gold
                c[4] += gold * gold
    if order:
        conn.execute(text("INSERT INTO cnt_order (patch, bracket, queue, champ_id, role, kind, value, games, wins) "
                          "VALUES (:patch, :b, :q, :c, :r, :k, :v, :g, :w)"),
                     [{"patch": patch, "b": b, "q": q, "c": c, "r": r, "k": k, "v": v, "g": g, "w": w}
                      for (b, q, c, r, k, v), (g, w) in order.items()])
    if lane:
        conn.execute(text("INSERT INTO cnt_lane (patch, bracket, queue, champ_id, role, opp_id, kind, value, games, "
                          "wins, gold_n, gold_sum, gold_sq) "
                          "VALUES (:patch, :b, :q, :c, :r, :o, :k, :v, :g, :w, :n, :s, :sq)"),
                     [{"patch": patch, "b": b, "q": q, "c": c, "r": r, "o": o, "k": k, "v": v, "g": g, "w": w,
                       "n": n, "s": s, "sq": sq} for (b, q, c, r, o, k, v), (g, w, n, s, sq) in lane.items()])


def _first_finished(purchases_json: str | None, static) -> int | None:
    for item, _t in json.loads(purchases_json or "[]"):
        it = static.items.get(item)
        if it is not None and it.finished:
            return item
    return None


def refresh_counts(db: Engine, patch: str, static=None) -> None:
    with db.begin() as conn:
        build_counts(conn, patch, static)


# ---------- everyone: counts -> runtime tables ----------
def derive(conn, patches: Iterable[str], window: str | None, weights: Mapping[str, float] | None = None) -> dict:
    """Rebuild the runtime tables of `patches` from the counts, then the synergy window ending at `window` and
    champions.roles. Returns matches per bracket of the last patch (what aggregate reports)."""
    from draftassist.data.synergy import aggregate_synergy, crawl_weights

    totals: dict[str, int] = {}
    for patch in patches:
        p = {"patch": patch}
        for t in ("champ_role_stats", "matchups", "bracket_totals"):
            conn.execute(text(f"DELETE FROM {t} WHERE patch = :patch"), p)
        conn.execute(text("""
            INSERT INTO bracket_totals (patch, bracket, matches)
            SELECT patch, bracket, SUM(matches) FROM cnt_bracket WHERE patch = :patch GROUP BY bracket"""), p)
        conn.execute(text("""
            INSERT INTO champ_role_stats (patch, bracket, champ_id, role, games, wins, picks, bans, dmg_phys, dmg_magic,
                                          dmg_true, games_short, wins_short, games_long, wins_long)
            SELECT patch, bracket, champ_id, role, SUM(games), SUM(wins), SUM(games),
                   COALESCE((SELECT SUM(b.bans) FROM cnt_ban b WHERE b.patch = cnt_role.patch
                             AND b.bracket = cnt_role.bracket AND b.champ_id = cnt_role.champ_id), 0),
                   SUM(dmg_phys_sum) / NULLIF(SUM(dmg_n), 0), SUM(dmg_magic_sum) / NULLIF(SUM(dmg_n), 0),
                   SUM(dmg_true_sum) / NULLIF(SUM(dmg_n), 0),
                   SUM(games_short), SUM(wins_short), SUM(games_long), SUM(wins_long)
            FROM cnt_role WHERE patch = :patch GROUP BY bracket, champ_id, role"""), p)
        conn.execute(text("""
            INSERT INTO matchups (patch, bracket, role, champ_a, champ_b, games, wins_a, lane_gold_sum, lane_xp_sum, lane_n)
            SELECT patch, bracket, role, champ_a, champ_b, SUM(games), SUM(wins_a), SUM(lane_gold_sum), SUM(lane_xp_sum),
                   SUM(lane_n)
            FROM cnt_matchup WHERE patch = :patch GROUP BY bracket, role, champ_a, champ_b"""), p)
        totals = dict(conn.execute(text("SELECT bracket, matches FROM bracket_totals WHERE patch = :patch"), p).all())
    if window is not None:
        aggregate_synergy(conn, window, weights if weights is not None else crawl_weights(conn, window))
        _champion_roles(conn, window)
    return totals


def _champion_roles(conn, patch: str) -> None:
    """champions.roles: roles with a meaningful share of the champion's games on `patch`, every tier pooled."""
    rows = conn.execute(text("SELECT champ_id, role, SUM(games) FROM champ_role_stats WHERE patch = :patch "
                             "GROUP BY champ_id, role"), {"patch": patch}).all()
    by_champ: dict[int, dict[str, int]] = {}
    for champ, role, games in rows:
        by_champ.setdefault(champ, {})[role] = games
    for champ, roles in by_champ.items():
        total = sum(roles.values())
        keep = sorted((r for r, g in roles.items() if g / total >= ROLE_SHARE_FOR_CHAMPION_ROLES), key=lambda r: -roles[r])
        conn.execute(text("UPDATE champions SET roles = :roles WHERE champ_id = :champ"),
                     {"roles": json.dumps(keep), "champ": champ})
