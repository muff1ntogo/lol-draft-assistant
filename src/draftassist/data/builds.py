"""Runes, items and summoner spells from crawled matches (docs/strategy.md).

Parsed from the match-v5 participant JSON the crawl already downloads (and caches on disk), stored in `match_builds`,
and read per champion and role for Live's Strategy view: what Master+ players run (the "what") and how it wins in our
player's bracket (the "how well"), optionally against one lane opponent. Item and rune names come from Data Dragon's
item.json / runesReforged.json / summoner.json (data/ddragon.fetch_static).
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, text
from sqlmodel import Session

from draftassist.data.synergy import window_weights

BUILD_HALF_LIFE = 1.0  # patches: builds change with every patch, so the previous one counts half
MIN_BRACKET_GAMES = 40  # games in the player's bracket before its win rates are shown; else every tier pooled
TOP_KEYSTONES = 3
TOP_ITEMS = 6
CORE_ITEMS = 3


def parse_build(p: Mapping) -> dict:
    """One participant's build, as stored (JSON columns as Python lists)."""
    perks = p.get("perks") or {}
    styles = {s.get("description"): s for s in perks.get("styles") or []}
    primary, sub = styles.get("primaryStyle") or {}, styles.get("subStyle") or {}
    runes = [sel.get("perk") for sel in (primary.get("selections") or [])] + \
            [sel.get("perk") for sel in (sub.get("selections") or [])]
    shard = perks.get("statPerks") or {}
    items = [p.get(f"item{i}") for i in range(6)]
    spells = sorted(x for x in (p.get("summoner1Id"), p.get("summoner2Id")) if x)
    return {"keystone": runes[0] if runes else None, "primary_style": primary.get("style"), "sub_style": sub.get("style"),
            "perks": [r for r in runes if r], "shards": [shard.get(k) for k in ("offense", "flex", "defense")],
            "items": [i for i in items if i], "spells": spells}


def parse_builds(participants: Iterable[Mapping]) -> tuple[dict, ...]:
    return tuple(parse_build(p) for p in participants)


def store_build(conn, match_id: str, champ_id: int, b: Mapping) -> None:
    conn.execute(text(
        "INSERT OR REPLACE INTO match_builds (match_id, champ_id, keystone, primary_style, sub_style, perks_json, "
        "shards_json, items_json, spells_json) VALUES (:m, :c, :k, :ps, :ss, :perks, :shards, :items, :spells)"),
        {"m": match_id, "c": champ_id, "k": b.get("keystone"), "ps": b.get("primary_style"), "ss": b.get("sub_style"),
         "perks": json.dumps(b.get("perks") or []), "shards": json.dumps(b.get("shards") or []),
         "items": json.dumps(b.get("items") or []), "spells": json.dumps(b.get("spells") or [])})


BACKFILL_COMMIT_EVERY = 2_000


def backfill_builds(db: Engine, cache_dir: Path) -> dict[str, int]:
    """Crawled matches without builds: filled from the on-disk Riot cache (no API calls), or counted as missing."""
    from draftassist.data.backfill import _cached_match

    updated = missing = 0
    with Session(db) as s:
        conn = s.connection()
        ids = [r[0] for r in conn.execute(text(
            "SELECT match_id FROM crawled_matches WHERE match_id NOT IN (SELECT DISTINCT match_id FROM match_builds) "
            "ORDER BY match_id"))]
        for match_id in ids:
            body = _cached_match(cache_dir, match_id)
            if not body:
                missing += 1
                continue
            for p in body["info"]["participants"]:
                store_build(conn, match_id, p["championId"], parse_build(p))
            updated += 1
            if updated % BACKFILL_COMMIT_EVERY == 0:
                s.commit()
                conn = s.connection()  # the commit closed the old one (backfills over 2,000 matches failed here)
        s.commit()
    return {"updated": updated, "missing": missing}


# ---------- reading ----------
@dataclass
class Choice:
    """One option (a keystone, an item, a rune page, a spell pair): how often the reference players take it (share of
    their games), and how it wins in the player's bracket."""
    key: object
    share: float  # weighted share of the reference set's games
    ref_games: float
    games: float = 0.0  # bracket games with it
    wins: float = 0.0

    @property
    def wr(self) -> float | None:
        return self.wins / self.games if self.games else None

    def as_dict(self) -> dict:
        return {"share": round(self.share, 3), "ref_games": round(self.ref_games), "games": round(self.games),
                "wr": None if self.wr is None else round(self.wr, 3)}


@dataclass
class BuildSummary:
    champ: int
    role: str
    opponent: int | None  # always None since decision 6: the overall build, with lane hints beside it
    ref_label: str  # "Master+" or the bracket's own games when there's no Master+ data
    ref_games: float
    bracket_games: float
    bracket_wr: float | None
    keystones: list[Choice] = field(default_factory=list)
    pages: list[Choice] = field(default_factory=list)  # full six-rune pages, the top one first
    items: list[Choice] = field(default_factory=list)  # every item held at the end, by share
    spells: list[Choice] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)  # "helpful in lane" vs the opponent (decision 6), usually none


# Counts per option, weighted by patch: {bracket: {kind: {value: [games, wins, raw games]}}} (cnt_build / cnt_order)
Counts = dict[str, dict[str, dict[str, list[float]]]]


def _weights(db: Engine, patch: str) -> dict[str, float]:
    from draftassist.data.counts import crawled_patches

    with db.connect() as conn:
        try:
            patches = crawled_patches(conn)
        except Exception:
            return {}
    return window_weights([*patches, patch], patch, BUILD_HALF_LIFE)


# The count tables are keyed (patch, bracket, queue, champion, role, ...): reading through the slices that exist
# (cnt_bracket), as the outer loop (CROSS JOIN keeps SQLite from reordering it), makes every lookup a key seek instead
# of a scan of the patch (db/optimize.hot_queries; tests/test_db_optimize.py checks the plans).
COUNTS_SQL = """WITH w(patch, weight) AS (VALUES {values}),
s AS MATERIALIZED (SELECT b.patch, b.bracket, b.queue, w.weight FROM cnt_bracket b JOIN w ON w.patch = b.patch)
SELECT c.bracket, c.kind, c.value, SUM(c.games * s.weight), SUM(c.wins * s.weight), SUM(c.games)
FROM s CROSS JOIN {table} c
WHERE c.patch = s.patch AND c.bracket = s.bracket AND c.queue = s.queue AND c.champ_id = :champ AND c.role = :role
GROUP BY c.bracket, c.kind, c.value"""
LANE_SQL = """WITH w(patch, weight) AS (VALUES {values}),
s AS MATERIALIZED (SELECT b.patch, b.bracket, b.queue, w.weight FROM cnt_bracket b JOIN w ON w.patch = b.patch)
SELECT c.kind, c.value, SUM(c.games * s.weight), SUM(c.wins * s.weight), SUM(c.gold_n * s.weight),
       SUM(c.gold_sum * s.weight), SUM(c.gold_sq * s.weight)
FROM s CROSS JOIN cnt_lane c
WHERE c.patch = s.patch AND c.bracket = s.bracket AND c.queue = s.queue AND c.champ_id = :champ AND c.role = :role
      AND c.opp_id = :opp
GROUP BY c.kind, c.value"""


def _weight_params(weights: Mapping[str, float], params: dict) -> str:
    """Binds the weights into `params`; returns the VALUES list."""
    for i, (pt, w) in enumerate(weights.items()):
        params |= {f"p{i}": pt, f"w{i}": w}
    return ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))


def _counts(db: Engine | None, table: str, champ: int, role: str, patch: str) -> Counts:
    out: Counts = defaultdict(lambda: defaultdict(dict))
    if db is None:
        return out
    weights = _weights(db, patch)
    if not weights:
        return out
    params: dict = {"champ": champ, "role": role}
    values = _weight_params(weights, params)
    with db.connect() as conn:
        try:
            rows = conn.execute(text(COUNTS_SQL.format(values=values, table=table)), params).all()
        except Exception:  # a database from before the count tables
            return out
    for bracket, kind, value, g, wins, raw in rows:
        out[bracket][kind][value] = [g, wins, raw]
    return out


def _pool(counts: Counts, brackets: Iterable[str] | None = None) -> dict[str, dict[str, list[float]]]:
    pooled: dict[str, dict[str, list[float]]] = defaultdict(dict)
    for b in sorted(counts):  # a fixed order: the float sums come out the same on every machine
        if brackets is not None and b not in brackets:
            continue
        for kind, values in counts[b].items():
            for v, c in values.items():
                acc = pooled[kind].setdefault(v, [0.0, 0.0, 0])
                for i in range(3):
                    acc[i] += c[i]
    return pooled


def _total(pooled: Mapping[str, Mapping[str, list[float]]]) -> float:
    return (pooled.get("total") or {}).get("", [0.0])[0]


def _sides(main: Counts, ref: Counts, bracket: str):
    """(reference, in-bracket, ref label): Master+ (else the bracket) says what; the bracket (else every tier) how well."""
    in_bracket = _pool(main, [bracket])
    if _total(in_bracket) < MIN_BRACKET_GAMES:
        in_bracket = _pool(main)
    reference, label = _pool(ref), "Master+"
    if _total(reference) <= 0:
        reference, label = in_bracket, "your bracket"
    return reference, in_bracket, label


def _choices(ref, mine, kind: str, top: int, convert=lambda v: v) -> list[Choice]:
    ref_total = _total(ref)
    options = sorted((ref.get(kind) or {}).items(), key=lambda kv: (-kv[1][0], kv[0]))[:top]
    out = []
    for value, (c, _w, _n) in options:
        g, w, _ = (mine.get(kind) or {}).get(value, [0.0, 0.0, 0])
        out.append(Choice(convert(value), c / ref_total if ref_total else 0.0, c, g, w))
    return out


def summarize(main: Engine, master: Engine | None, patch: str, champ: int, role: str, bracket: str,
              opponent: int | None = None) -> BuildSummary | None:
    """What to run on `champ` in `role`: Master+ games (when there's a Master+ database) for the options and their
    shares, the main database's games in `bracket` (every tier when that's too few) for their win rates. From the count
    tables, so a teammate's pulled data gives the same. Always the overall build; with `opponent`, the "helpful in lane"
    hints against them (decision 6). None without any games."""
    ref, mine, label = _sides(_counts(main, "cnt_build", champ, role, patch),
                              _counts(master, "cnt_build", champ, role, patch), bracket)
    if _total(ref) <= 0:
        return None
    games = _total(mine)
    wins = (mine.get("total") or {}).get("", [0.0, 0.0])[1]
    summary = BuildSummary(champ, role, None, label, _total(ref), games, wins / games if games else None)
    summary.keystones = _choices(ref, mine, "keystone", TOP_KEYSTONES, int)
    summary.pages = _choices(ref, mine, "page", 1)
    summary.items = _choices(ref, mine, "item", 30, int)
    summary.spells = _choices(ref, mine, "spells", 1)
    if opponent is not None:
        exclude = {("rune", r) for p in summary.pages for r in json.loads(p.key)}
        exclude |= {("first", c.key) for c in summary.items[:CORE_ITEMS]}
        order = _pool(_counts(main, "cnt_order", champ, role, patch))
        starter = max((order.get("starter") or {}).items(), key=lambda kv: (kv[1][0], kv[0]), default=None)
        if starter is not None:
            exclude |= {("start", i) for i in json.loads(starter[0])}
        summary.hints = lane_hints(main, patch, champ, role, opponent, exclude)
    return summary


# ---------- "helpful in lane" hints (decision 6; the rule agreed 8 Nov 2026) ----------
HINT_MIN_GAMES = 30  # weighted games with the option, and without it, against this opponent
HINT_MIN_GOLD = 300.0  # gold at 15 over the same champion's other games against them ...
HINT_MIN_WR = 0.03  # ... or win rate points
HINT_MIN_Z = 3.0  # and this many standard errors: about 0.1 false hints per lane from ~70 options tested
HINT_MAX = 2
HINT_KINDS = {"rune": "rune_total", "start": "item_total", "first": "item_total"}


@dataclass(frozen=True)
class LaneStats:
    """One option's games against one opponent: games and wins, and the gold lead at 15 as count, sum, sum of squares."""
    games: float
    wins: float
    gold_n: float
    gold_sum: float
    gold_sq: float

    def _t(self) -> tuple:
        return self.games, self.wins, self.gold_n, self.gold_sum, self.gold_sq

    def __add__(self, o: LaneStats) -> LaneStats:
        return LaneStats(*(a + b for a, b in zip(self._t(), o._t())))

    def __sub__(self, o: LaneStats) -> LaneStats:
        return LaneStats(*(a - b for a, b in zip(self._t(), o._t())))


@dataclass(frozen=True)
class Hint:
    kind: str  # rune, start (a starting item), first (the first finished item)
    value: int
    measure: str  # "gold" (lead at 15) or "win" (win rate)
    gain: float  # gold, or win-rate points as a fraction
    z: float
    games: float  # weighted games with it against this opponent
    others: float  # ... and without it


def lane_hint(with_it: LaneStats, total: LaneStats) -> Hint | None:
    """The rule: HINT_MIN_GAMES with and without the option, and a gain of HINT_MIN_GOLD at 15 or HINT_MIN_WR win rate at
    HINT_MIN_Z standard errors; the better z when both pass. kind and value are filled in by lane_hints."""
    a, b = with_it, total - with_it
    if a.games < HINT_MIN_GAMES or b.games < HINT_MIN_GAMES:
        return None
    found: list[tuple[float, str, float]] = []
    if a.gold_n >= HINT_MIN_GAMES and b.gold_n >= HINT_MIN_GAMES:
        ma, mb = a.gold_sum / a.gold_n, b.gold_sum / b.gold_n
        va, vb = max(a.gold_sq / a.gold_n - ma * ma, 0.0), max(b.gold_sq / b.gold_n - mb * mb, 0.0)
        se = math.sqrt(va / a.gold_n + vb / b.gold_n)
        if se > 0 and ma - mb >= HINT_MIN_GOLD and (ma - mb) / se >= HINT_MIN_Z:
            found.append(((ma - mb) / se, "gold", ma - mb))
    pa, pb = a.wins / a.games, b.wins / b.games
    p = (a.wins + b.wins) / (a.games + b.games)
    se = math.sqrt(p * (1 - p) * (1 / a.games + 1 / b.games))
    if se > 0 and pa - pb >= HINT_MIN_WR and (pa - pb) / se >= HINT_MIN_Z:
        found.append(((pa - pb) / se, "win", pa - pb))
    if not found:
        return None
    z, measure, gain = max(found)
    return Hint("", 0, measure, gain, z, a.games, b.games)


def lane_hints(db: Engine, patch: str, champ: int, role: str, opponent: int, exclude: set) -> list[Hint]:
    """At most HINT_MAX hints against `opponent`, every tier of the database pooled, patches weighted as the builds are.
    `exclude`: (kind, value) already in the overall build, which aren't hints."""
    weights = _weights(db, patch)
    if not weights:
        return []
    params: dict = {"champ": champ, "role": role, "opp": opponent}
    values = _weight_params(weights, params)
    with db.connect() as conn:
        try:
            rows = conn.execute(text(LANE_SQL.format(values=values)), params).all()
        except Exception:  # a database from before the count tables
            return []
    stats = {(k, v): LaneStats(*(float(x or 0) for x in rest)) for k, v, *rest in rows}
    hints = []
    for (kind, value), s in stats.items():
        total = stats.get((HINT_KINDS.get(kind), ""))
        if total is None or (kind, int(value)) in exclude:
            continue
        h = lane_hint(s, total)
        if h is not None:
            hints.append(Hint(kind, int(value), h.measure, h.gain, h.z, h.games, h.others))
    return sorted(hints, key=lambda h: (-h.z, h.kind, h.value))[:HINT_MAX]


# ---------- builds in order, from timelines (docs/top-lane-power.md, Stage 3) ----------
ORDER_MIN_GAMES = 50  # an option is shown with at least this many (weighted) reference games ...
ORDER_MIN_SHARE = 0.05  # ... and this share of them
STARTER_S = 60  # purchases in the first minute are the starting items


def skill_max(skills: str) -> str | None:
    """"QWE" = Q maxed first, then W, then E: by when each basic ability got its 5th point (else by points at the end)."""
    if not skills:
        return None
    reached, counts = {}, {k: 0 for k in "QWE"}
    for i, k in enumerate(skills):
        if k in counts:
            counts[k] += 1
            if counts[k] == 5 and k not in reached:
                reached[k] = i
    return "".join(sorted("QWE", key=lambda k: (reached.get(k, 99), -counts[k], "QWE".index(k))))


def _features(row: tuple, static: GameStatic) -> dict:
    purchases = json.loads(row[0] or "[]")
    finished, boots, seen = [], None, set()
    for item, t in purchases:
        it = static.items.get(item)
        if it is None or item in seen:
            continue
        seen.add(item)
        if it.boots and boots is None:
            boots = item
        elif it.finished:
            finished.append((item, t))
    levels = {int(k): v for k, v in json.loads(row[2] or "{}").items()}
    return {"core": tuple(i for i, _ in finished[:2]) if len(finished) >= 2 else None,
            "first_item_s": finished[0][1] if finished else None, "boots": boots,
            "starter": tuple(sorted(i for i, t in purchases if t <= STARTER_S)) or None,
            "first3": row[1][:3] if len(row[1] or "") >= 3 else None, "max": skill_max(row[1] or ""),
            "level6_s": levels.get(6)}


def _binned_median(bins: Mapping[str, list[float]]) -> float | None:
    """The median of seconds counted in TIME_BIN_S bins (raw games, as the per-row median was): the bin's middle."""
    from draftassist.data.counts import TIME_BIN_S

    items = sorted((int(v), c[2]) for v, c in bins.items())
    n = sum(c for _, c in items)
    seen = 0
    for start, c in items:
        seen += c
        if seen > n // 2:
            return start + TIME_BIN_S / 2
    return None


_ORDER_VALUE = {"core": lambda v: tuple(json.loads(v)), "starter": lambda v: tuple(json.loads(v)), "boots": int}


def summarize_order(main: Engine, master: Engine | None, patch: str, champ: int, role: str, bracket: str,
                    static: GameStatic) -> dict | None:
    """Item order (the first two finished items, as an ordered pair and how often that order holds within the pair),
    starting items, skill max and first three skill points, with the median second of the first finished item and of
    level 6: what Master+ does (else the player's bracket), and how it wins in the player's bracket. Gated: an option
    needs ORDER_MIN_GAMES reference games and ORDER_MIN_SHARE of them. From the count tables (cnt_order). None without
    item names or games."""
    if not static.items:
        return None
    ref, mine, label = _sides(_counts(main, "cnt_order", champ, role, patch),
                              _counts(master, "cnt_order", champ, role, patch), bracket)
    total = _total(ref)
    if total <= 0:
        return None

    def best(key: str) -> dict | None:
        options = ref.get(key) or {}
        if not options:
            return None
        raw, (n, _w, _r) = max(options.items(), key=lambda kv: (kv[1][0], kv[0]))
        if n < ORDER_MIN_GAMES or n / total < ORDER_MIN_SHARE:
            return None
        g, wins, _ = (mine.get(key) or {}).get(raw, [0.0, 0.0, 0])
        return {"value": _ORDER_VALUE.get(key, lambda v: v)(raw), "share": round(n / total, 3), "ref_games": round(n),
                "games": round(g), "wr": round(wins / g, 3) if g else None}

    core = best("core")
    if core is not None:  # the pair in either order: how often this order holds
        both = (ref.get("core_set") or {}).get(json.dumps(sorted(core["value"])), [0.0])[0]
        core["order_share"] = round(core["ref_games"] / both, 3) if both else None
        core["names"] = [static.item_name(i) for i in core["value"]]
    boots, starter = best("boots"), best("starter")
    if boots is not None:
        boots["names"] = [static.item_name(boots["value"])]
    if starter is not None:
        starter["names"] = [static.item_name(i) for i in starter["value"]]
    return {"ref_label": label, "ref_games": round(total), "core": core, "boots": boots, "starter": starter,
            "skill_max": best("max"), "first3": best("first3"),
            "first_item_s": _binned_median(ref.get("first_item_s") or {}),
            "level6_s": _binned_median(ref.get("level6_s") or {})}


def most_common(values: Iterable) -> object | None:
    c = Counter(v for v in values if v is not None)
    return c.most_common(1)[0][0] if c else None


# ---------- what counts as a damage-heavy team (decision 8 Nov 2026: from the data, not hand-picked) ----------
HEAVY_PERCENTILE = 0.8  # a team more physical (or magic) than 80% of crawled teams is "heavy"
MIN_TEAMS = 200  # fewer crawled teams with damage: keep engine/strategy's defaults
RECENT_PATCHES = 3


@dataclass(frozen=True)
class DamageThresholds:
    ad_heavy: float  # physical share of a team's damage at HEAVY_PERCENTILE
    ap_heavy: float  # magic share at HEAVY_PERCENTILE
    teams: int
    median_ad: float
    median_ap: float


def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    i = q * (len(xs) - 1)
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def damage_thresholds(db: Engine, limit_teams: int = 60_000) -> DamageThresholds | None:
    """Physical and magic share of each crawled team's damage to champions (the mean of its five players' shares, as the
    Strategy view reads a team), over the last RECENT_PATCHES patches; the HEAVY_PERCENTILE of each. None with too few."""
    with db.connect() as conn:
        try:
            patches = [r[0] for r in conn.execute(text("SELECT DISTINCT patch FROM crawled_matches"))]
        except Exception:
            return None
        recent = sorted(patches, key=_version_key, reverse=True)[:RECENT_PATCHES]
        if not recent:
            return None
        marks = ", ".join(f":p{i}" for i in range(len(recent)))
        rows = conn.execute(text(
            f"SELECT p.match_id, p.team_id, p.dmg_phys, p.dmg_magic, p.dmg_true FROM match_participants p "
            f"JOIN crawled_matches m ON m.match_id = p.match_id WHERE m.patch IN ({marks}) AND p.dmg_phys IS NOT NULL "
            f"ORDER BY m.game_start_ms DESC LIMIT :n"), {**{f"p{i}": p for i, p in enumerate(recent)},
                                                         "n": limit_teams * 5}).all()
    teams: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for match_id, team, phys, magic, true in rows:
        total = (phys or 0) + (magic or 0) + (true or 0)
        if total > 0:
            teams[(match_id, team)].append((phys / total, magic / total))
    ad = [sum(x for x, _ in v) / len(v) for v in teams.values() if len(v) == 5]
    ap = [sum(y for _, y in v) / len(v) for v in teams.values() if len(v) == 5]
    if len(ad) < MIN_TEAMS:
        return None
    return DamageThresholds(round(_quantile(ad, HEAVY_PERCENTILE), 3), round(_quantile(ap, HEAVY_PERCENTILE), 3),
                            len(ad), round(_quantile(ad, 0.5), 3), round(_quantile(ap, 0.5), 3))


def load_damage_thresholds(db: Engine, window: str | None = None) -> DamageThresholds | None:
    """The stored thresholds for `window` (else the newest): aggregation writes them, a pull brings them."""
    with db.connect() as conn:
        try:
            rows = conn.execute(text('SELECT "window", ad_heavy, ap_heavy, teams, median_ad, median_ap '
                                     "FROM damage_thresholds")).all()
        except Exception:  # a database from before the table
            return None
    if not rows:
        return None
    row = next((r for r in rows if r[0] == window), None) or max(rows, key=lambda r: _version_key(r[0]))
    return DamageThresholds(ad_heavy=row[1], ap_heavy=row[2], teams=row[3], median_ad=row[4], median_ap=row[5])


def main() -> int:
    """`uv run python -m draftassist.data.builds`: what the crawl says a damage-heavy team is, and how many matches have
    builds stored."""
    from draftassist.config import load_settings
    from draftassist.db.session import init_db

    db = init_db(load_settings().db_path)
    t = damage_thresholds(db)
    if t is None:
        print(f"Too few crawled teams with damage (need {MIN_TEAMS}): the Strategy view keeps its defaults.")
    else:
        print(f"{t.teams:,} crawled teams, last {RECENT_PATCHES} patches. Median team: {t.median_ad:.0%} physical, "
              f"{t.median_ap:.0%} magic.")
        print(f"Damage-heavy (top {1 - HEAVY_PERCENTILE:.0%}): {t.ad_heavy:.0%}+ physical, {t.ap_heavy:.0%}+ magic.")
    with db.connect() as conn:
        n = conn.execute(text("SELECT COUNT(DISTINCT match_id) FROM match_builds")).scalar()
    print(f"Matches with runes and items stored: {n:,} (data.backfill fills older ones from the cache).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------- names (Data Dragon) ----------
LEGENDARY_GOLD = 2000  # a finished item costs at least this much (boots and support items are told apart separately)


@dataclass(frozen=True)
class Item:
    id: int
    name: str
    gold: int
    tags: tuple[str, ...]
    boots: bool  # tier-2 boots
    finished: bool  # a legendary: builds into nothing, costs LEGENDARY_GOLD+
    anti_heal: bool  # applies Grievous Wounds
    stats: tuple[tuple[str, float], ...] = ()  # Data Dragon's flat stats, e.g. ("FlatPhysicalDamageMod", 40.0)


@dataclass
class GameStatic:
    """Item, rune and summoner spell names for one Data Dragon version; empty when the files aren't cached."""
    version: str | None = None
    items: dict[int, Item] = field(default_factory=dict)
    runes: dict[int, str] = field(default_factory=dict)  # rune and rune-tree ids -> name
    spells: dict[int, str] = field(default_factory=dict)  # summoner spell key -> name

    def item_name(self, i: int) -> str:
        return self.items[i].name if i in self.items else f"item {i}"

    def rune_name(self, r: int | None) -> str:
        return self.runes.get(r, f"rune {r}") if r else "—"

    def spell_name(self, s: int) -> str:
        return self.spells.get(s, f"spell {s}")


def _version_key(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def load_static(cache_dir: Path, version: str | None = None, locale: str = "en_US") -> GameStatic:
    """From the Data Dragon cache: `version`, else the newest version cached with item.json."""
    root = cache_dir / "ddragon"
    if version is None or not (root / version / locale / "item.json").is_file():
        cached = [p.parent.parent.name for p in root.glob(f"*/{locale}/item.json")] if root.is_dir() else []
        if not cached:
            return GameStatic()
        version = max(cached, key=_version_key)
    base = root / version / locale
    out = GameStatic(version)

    def read(name: str):
        f = base / name
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None

    items = read("item.json") or {}
    for key, it in (items.get("data") or {}).items():
        tags = tuple(it.get("tags") or ())
        gold = int((it.get("gold") or {}).get("total") or 0)
        boots = "Boots" in tags and int(it.get("depth") or 1) >= 2
        finished = not it.get("into") and gold >= LEGENDARY_GOLD and not boots and "Consumable" not in tags
        anti = "grievous wounds" in (it.get("description") or "").lower()
        stats = tuple(sorted((k, float(v)) for k, v in (it.get("stats") or {}).items() if isinstance(v, (int, float))))
        out.items[int(key)] = Item(int(key), it.get("name", key), gold, tags, boots, finished, anti, stats)
    for tree in read("runesReforged.json") or []:
        out.runes[tree["id"]] = tree["name"]
        for slot in tree.get("slots") or []:
            for r in slot.get("runes") or []:
                out.runes[r["id"]] = r["name"]
    for sp in ((read("summoner.json") or {}).get("data") or {}).values():
        out.spells[int(sp["key"])] = sp["name"]
    return out
