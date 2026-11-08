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
MIN_OPP_GAMES = 30  # games against one lane opponent before a vs-opponent build replaces the overall one
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
            if updated % 2000 == 0:
                s.commit()
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
    opponent: int | None  # set when the vs-opponent sample was big enough
    ref_label: str  # "Master+" or the bracket's own games when there's no Master+ data
    ref_games: float
    bracket_games: float
    bracket_wr: float | None
    keystones: list[Choice] = field(default_factory=list)
    pages: list[Choice] = field(default_factory=list)  # full six-rune pages, the top one first
    items: list[Choice] = field(default_factory=list)  # every item held at the end, by share
    spells: list[Choice] = field(default_factory=list)


_ROWS_SQL = """
WITH w(patch, weight) AS (VALUES {values})
SELECT b.keystone, b.perks_json, b.items_json, b.spells_json, p.win, w.weight, m.bracket
FROM match_builds b
JOIN match_participants p ON p.match_id = b.match_id AND p.champ_id = b.champ_id
JOIN crawled_matches m ON m.match_id = b.match_id
JOIN w ON w.patch = m.patch
{opp_join}
WHERE b.champ_id = :champ AND p.role = :role {opp_where}
"""


def _rows(db: Engine, champ: int, role: str, weights: Mapping[str, float], opponent: int | None) -> list[tuple]:
    if not weights:
        return []
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    params: dict = {"champ": champ, "role": role}
    for i, (patch, w) in enumerate(weights.items()):
        params |= {f"p{i}": patch, f"w{i}": w}
    opp_join = opp_where = ""
    if opponent is not None:
        opp_join = ("JOIN match_participants o ON o.match_id = b.match_id AND o.role = p.role "
                    "AND o.team_id <> p.team_id")
        opp_where = "AND o.champ_id = :opp"
        params["opp"] = opponent
    sql = _ROWS_SQL.format(values=values, opp_join=opp_join, opp_where=opp_where)
    with db.connect() as conn:
        try:
            return list(conn.execute(text(sql), params))
        except Exception:  # a database from before match_builds existed
            return []


def _weights(db: Engine, patch: str) -> dict[str, float]:
    with db.connect() as conn:
        try:
            patches = [r[0] for r in conn.execute(text("SELECT DISTINCT patch FROM crawled_matches"))]
        except Exception:
            return {}
    return window_weights([*patches, patch], patch, BUILD_HALF_LIFE)


def _choices(ref: list[tuple], bracket: list[tuple], key_of, top: int, multi: bool = False) -> list[Choice]:
    ref_total = sum(r[5] for r in ref)
    counts: dict = defaultdict(float)
    for r in ref:
        for k in (key_of(r) if multi else [key_of(r)]):
            if k is not None:
                counts[k] += r[5]
    out = []
    for k, c in sorted(counts.items(), key=lambda kv: -kv[1])[:top]:
        ch = Choice(k, c / ref_total if ref_total else 0.0, c)
        for r in bracket:
            keys = key_of(r) if multi else [key_of(r)]
            if k in keys:
                ch.games += r[5]
                ch.wins += r[5] * bool(r[4])
        out.append(ch)
    return out


def summarize(main: Engine, master: Engine | None, patch: str, champ: int, role: str, bracket: str,
              opponent: int | None = None) -> BuildSummary | None:
    """What to run on `champ` in `role`: Master+ games (when there's a Master+ database) for the options and their
    shares, the main database's games in `bracket` (every tier when that's too few) for their win rates. Against
    `opponent` when both have MIN_OPP_GAMES there; otherwise overall. None without any games."""
    def load(db: Engine | None, opp: int | None) -> list[tuple]:
        return [] if db is None else _rows(db, champ, role, _weights(db, patch), opp)

    used_opp = None
    main_rows, ref_rows = load(main, None), load(master, None)
    if opponent is not None:
        main_opp, ref_opp = load(main, opponent), load(master, opponent)
        if sum(r[5] for r in main_opp) >= MIN_OPP_GAMES and (master is None or sum(r[5] for r in ref_opp) >= MIN_OPP_GAMES):
            main_rows, ref_rows, used_opp = main_opp, ref_opp, opponent
    in_bracket = [r for r in main_rows if r[6] == bracket]
    if sum(r[5] for r in in_bracket) < MIN_BRACKET_GAMES:
        in_bracket = main_rows
    ref_label = "Master+"
    if not ref_rows:
        ref_rows, ref_label = in_bracket, "your bracket"
    if not ref_rows:
        return None
    games = sum(r[5] for r in in_bracket)
    summary = BuildSummary(champ, role, used_opp, ref_label, sum(r[5] for r in ref_rows), games,
                           (sum(r[5] * bool(r[4]) for r in in_bracket) / games) if games else None)
    summary.keystones = _choices(ref_rows, in_bracket, lambda r: r[0], TOP_KEYSTONES)
    summary.pages = _choices(ref_rows, in_bracket, lambda r: r[1] if r[1] and r[1] != "[]" else None, 1)
    summary.items = _choices(ref_rows, in_bracket, lambda r: json.loads(r[2] or "[]"), 30, multi=True)
    summary.spells = _choices(ref_rows, in_bracket, lambda r: r[3] if r[3] and r[3] != "[]" else None, 1)
    return summary


# ---------- builds in order, from timelines (docs/top-lane-power.md, Stage 3) ----------
ORDER_MIN_GAMES = 50  # an option is shown with at least this many (weighted) reference games ...
ORDER_MIN_SHARE = 0.05  # ... and this share of them
STARTER_S = 60  # purchases in the first minute are the starting items

_ORDER_SQL = """
WITH w(patch, weight) AS (VALUES {values})
SELECT e.purchases_json, e.skills, e.levels_json, p.win, w.weight, m.bracket
FROM match_build_events e
JOIN match_participants p ON p.match_id = e.match_id AND p.champ_id = e.champ_id
JOIN crawled_matches m ON m.match_id = e.match_id
JOIN w ON w.patch = m.patch
WHERE e.champ_id = :champ AND p.role = :role
"""


def _order_rows(db: Engine | None, champ: int, role: str, patch: str) -> list[tuple]:
    if db is None:
        return []
    weights = _weights(db, patch)
    if not weights:
        return []
    values = ", ".join(f"(:p{i}, :w{i})" for i in range(len(weights)))
    params: dict = {"champ": champ, "role": role}
    for i, (pt, w) in enumerate(weights.items()):
        params |= {f"p{i}": pt, f"w{i}": w}
    with db.connect() as conn:
        try:
            return list(conn.execute(text(_ORDER_SQL.format(values=values)), params))
        except Exception:  # a database from before match_build_events existed
            return []


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


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


def summarize_order(main: Engine, master: Engine | None, patch: str, champ: int, role: str, bracket: str,
                    static: GameStatic) -> dict | None:
    """Item order (the first two finished items, as an ordered pair and how often that order holds within the pair),
    starting items, skill max and first three skill points, with the median second of the first finished item and of
    level 6: what Master+ does (else the player's bracket), and how it wins in the player's bracket. Gated: an option
    needs ORDER_MIN_GAMES reference games and ORDER_MIN_SHARE of them. None without item names or games."""
    if not static.items:
        return None
    main_rows = _order_rows(main, champ, role, patch)
    ref_rows = _order_rows(master, champ, role, patch) if master is not None else []
    in_bracket = [r for r in main_rows if r[5] == bracket]
    if sum(r[4] for r in in_bracket) < MIN_BRACKET_GAMES:
        in_bracket = main_rows
    ref_label = "Master+"
    if not ref_rows:
        ref_rows, ref_label = in_bracket, "your bracket"
    total = sum(r[4] for r in ref_rows)
    if total <= 0:
        return None
    ref = [(_features(r, static), r[4]) for r in ref_rows]
    mine = [(_features(r, static), r[4], bool(r[3])) for r in in_bracket]

    def best(key: str) -> dict | None:
        counts: dict = defaultdict(float)
        for f, w in ref:
            if f[key] is not None:
                counts[f[key]] += w
        if not counts:
            return None
        value, n = max(counts.items(), key=lambda kv: kv[1])
        if n < ORDER_MIN_GAMES or n / total < ORDER_MIN_SHARE:
            return None
        g = sum(w for f, w, _ in mine if f[key] == value)
        wins = sum(w for f, w, won in mine if f[key] == value and won)
        return {"value": value, "share": round(n / total, 3), "ref_games": round(n), "games": round(g),
                "wr": round(wins / g, 3) if g else None}

    core = best("core")
    if core is not None:  # the pair in either order: how often this order holds
        pair = set(core["value"])
        both = sum(w for f, w in ref if f["core"] is not None and set(f["core"]) == pair)
        core["order_share"] = round(core["ref_games"] / both, 3) if both else None
        core["names"] = [static.item_name(i) for i in core["value"]]
    boots, starter = best("boots"), best("starter")
    if boots is not None:
        boots["names"] = [static.item_name(boots["value"])]
    if starter is not None:
        starter["names"] = [static.item_name(i) for i in starter["value"]]
    return {"ref_label": ref_label, "ref_games": round(total), "core": core, "boots": boots, "starter": starter,
            "skill_max": best("max"), "first3": best("first3"),
            "first_item_s": _median([f["first_item_s"] for f, _ in ref]),
            "level6_s": _median([f["level6_s"] for f, _ in ref])}


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
        out.items[int(key)] = Item(int(key), it.get("name", key), gold, tags, boots, finished, anti)
    for tree in read("runesReforged.json") or []:
        out.runes[tree["id"]] = tree["name"]
        for slot in tree.get("slots") or []:
            for r in slot.get("runes") or []:
                out.runes[r["id"]] = r["name"]
    for sp in ((read("summoner.json") or {}).get("data") or {}).values():
        out.spells[int(sp["key"])] = sp["name"]
    return out
