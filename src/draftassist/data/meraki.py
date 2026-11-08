"""Champion stats and ability values from Meraki Analytics' `lolstaticdata` (MIT licence; parsed from the League of
Legends Wiki; docs/top-lane-power.md, Stage 4, user decision 8 Nov 2026).

One file for every champion (~13 MB), downloaded once and cached under `cache/meraki/`; refreshed when a week old.
Only what the power model needs is kept: base stats with per-level growth, and per ability its damage per rank (each
part's type, flat values and ratios) and cooldowns. Abilities are read with simple rules (`_ability_damage`), so some
kits are only roughly modelled; each ability says whether its damage was read (`modelled`).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

URL = "https://cdn.merakianalytics.com/riot/lol/resources/latest/en-US/champions.json"
CACHE_TTL_S = 7 * 86_400
STATS = ("health", "armor", "magicResistance", "attackDamage", "attackSpeed", "attackSpeedRatio", "attackRange")
# ratio units we model; anything else is dropped (and the ability marked partly modelled)
UNITS = {"": "flat", "% AD": "ad", "% bonus AD": "bonus_ad", "% AP": "ap", "% of target's maximum health": "target_hp",
         "% of the target's maximum health": "target_hp", "% maximum health": "own_hp", "% bonus health": "bonus_hp",
         "% of target's current health": "target_hp", "% of target's missing health": "target_missing_hp"}
SKIP = ("minion", "monster", "non-champion", "reduced", "reduction", "increased", "capped", "modified", "enhanced",
        "empowered", "amplif", "modifier")  # damage lines that aren't a cast's damage to a champion
TOTAL_OVER_TIME_IN_TRADE = 0.4  # a "total" over a long effect (with a per-tick line too): the share landing in a trade


@dataclass(frozen=True)
class DamagePart:
    kind: str  # physical | magic | true
    per_rank: tuple[tuple[str, tuple[float, ...]], ...]  # (unit, values per rank), unit from UNITS


@dataclass(frozen=True)
class Ability:
    key: str  # Q W E R
    name: str
    cooldowns: tuple[float, ...]
    parts: tuple[DamagePart, ...] = ()
    modelled: bool = True  # False: some damage couldn't be read


@dataclass(frozen=True)
class ChampionKit:
    key: str  # Data Dragon key
    champ_id: int
    stats: dict[str, tuple[float, float]]  # stat -> (flat, per level); attackSpeed per level is a percentage
    abilities: dict[str, Ability] = field(default_factory=dict)
    ranged: bool = False


def fetch_champions(cache_dir: Path, client: httpx.Client | None = None, clock=time.time) -> dict:
    path = Path(cache_dir) / "meraki" / "champions.json"
    if path.is_file() and clock() - path.stat().st_mtime < CACHE_TTL_S:
        return json.loads(path.read_text(encoding="utf-8"))
    own = client is None
    client = client or httpx.Client(timeout=120)
    try:
        resp = client.get(URL)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.text, encoding="utf-8")
        return resp.json()
    except httpx.HTTPError:
        if path.is_file():  # stale is better than nothing
            log.warning("Meraki unreachable; using the cached champion data")
            return json.loads(path.read_text(encoding="utf-8"))
        raise
    finally:
        if own:
            client.close()


def _kind(attribute: str, fallback: str | None) -> str | None:
    a = attribute.lower()
    for k in ("physical", "magic", "true"):
        if k in a:
            return k
    return {"PHYSICAL_DAMAGE": "physical", "MAGIC_DAMAGE": "magic", "TRUE_DAMAGE": "true"}.get(fallback or "")


def _part(leveling: dict, fallback: str | None, ranks: int) -> tuple[DamagePart | None, bool]:
    kind = _kind(leveling.get("attribute", ""), fallback)
    if kind is None:
        return None, False
    per_rank, whole = [], True
    per_second = "per second" in leveling["attribute"].lower()
    for m in leveling.get("modifiers", []):
        unit = UNITS.get(" ".join(((m.get("units") or [""])[0]).split()))
        values = [float(v) for v in (m.get("values") or [])][:ranks]
        if unit is None or not values:
            whole = False
            continue
        if per_second:
            values = [v * 2 for v in values]  # a damage-over-time: two seconds of it in a trade
        per_rank.append((unit, tuple(values)))
    return (DamagePart(kind, tuple(per_rank)) if per_rank else None), whole


def _ability_damage(ability: dict, ranks: int) -> tuple[tuple[DamagePart, ...], bool]:
    """The ability's first effect with damage to champions: a Minimum/Maximum pair averages; a "Total" line wins over the
    others, except that a total spread over a long effect (one with a per-tick or per-second line too) counts at
    TOTAL_OVER_TIME_IN_TRADE. Later effects are usually the empowered or alternative version and aren't added."""
    whole = True
    for effect in ability.get("effects", []):
        lines = [lv for lv in effect.get("leveling", [])
                 if "damage" in lv.get("attribute", "").lower()
                 and not any(w in lv["attribute"].lower() for w in SKIP)]
        if not lines:
            continue
        name = lambda lv: lv["attribute"].lower()  # noqa: E731
        lows = [lv for lv in lines if name(lv).startswith("minimum")]
        highs = [lv for lv in lines if name(lv).startswith("maximum")]
        totals = [lv for lv in lines if name(lv).startswith("total")]
        over_time = any(("per tick" in name(lv) or "per second" in name(lv)) for lv in lines)
        if lows and highs:
            lo, ok1 = _part(lows[0], ability.get("damageType"), ranks)
            hi, ok2 = _part(highs[0], ability.get("damageType"), ranks)
            if lo and hi:
                merged = tuple((u, tuple((x + y) / 2 for x, y in zip(a, b)))
                               for (u, a), (_, b) in zip(lo.per_rank, hi.per_rank))
                return (DamagePart(lo.kind, merged),), ok1 and ok2
            continue
        if totals:
            part, ok = _part(totals[0], ability.get("damageType"), ranks)
            if part and over_time:
                part = DamagePart(part.kind, tuple((u, tuple(v * TOTAL_OVER_TIME_IN_TRADE for v in vals))
                                                   for u, vals in part.per_rank))
        else:
            part, ok = _part(lines[0], ability.get("damageType"), ranks)
        if part:
            return (part,), ok
        whole = False
    return (), whole


def parse_kit(raw: dict) -> ChampionKit:
    stats = {}
    for s in STATS:
        v = raw.get("stats", {}).get(s) or {}
        stats[s] = (float(v.get("flat") or 0.0), float(v.get("perLevel") or 0.0))
    abilities = {}
    for key in "QWER":
        variants = (raw.get("abilities") or {}).get(key) or []
        if not variants:
            continue
        a = variants[0]
        ranks = 3 if key == "R" else 5
        cd = (((a.get("cooldown") or {}).get("modifiers") or [{}])[0].get("values") or [])[:ranks]
        parts, whole = _ability_damage(a, ranks)
        abilities[key] = Ability(key, a.get("name", key), tuple(float(c) for c in cd), parts, whole)
    return ChampionKit(raw.get("key", ""), int(raw.get("id", 0)), stats, abilities,
                       (raw.get("attackType") or "").upper() == "RANGED")


def load_kits(cache_dir: Path, client: httpx.Client | None = None) -> dict[int, ChampionKit]:
    """champion id -> kit, for every champion Meraki has."""
    data = fetch_champions(cache_dir, client)
    out = {}
    for raw in data.values():
        try:
            kit = parse_kit(raw)
        except Exception:
            log.warning("Meraki: couldn't read %s", raw.get("key"), exc_info=True)
            continue
        out[kit.champ_id] = kit
    return out
