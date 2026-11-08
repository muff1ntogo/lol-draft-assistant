"""Lane power by milestone (docs/top-lane-power.md, Stages 4-5). Pure: kits (data/meraki.py), item stats (Data Dragon)
and builds come in; nothing is read here.

**Theory (Stage 4).** For a champion in a state (level, finished items, ability ranks), a duel score
    DP = sqrt(DPS x EHP)
against a reference opponent: the average of the role's champions in the same state. DPS is a 6-second trade: auto
attacks plus every ability with damage on its cooldown (no haste), mitigated by the reference's armour and magic resist;
EHP is health over the damage taken per point, for a 60 / 40 physical / magic mix. The curve is DP over the role's
average DP at that milestone: 1.0 = an average laner of the role. A spike is a rise from the previous milestone more
than one standard deviation above the role's average rise there. Item passives, shields, healing, crowd control and
mobility aren't modelled: kits where those matter most read low (the doc says so).

**Measured (Stage 5).** The gold lead gained per minute over the lane opponent while in each milestone state, against
the role's average there (data/power.py does the counting). Combined with the theory on one scale (gold per minute):
theory is mapped to gold/minute by a least-squares slope over every cell with data, then
    combined = w x measured + (1 - w) x slope x (theory - 1),   w = n / (n + SHRINK_MINUTES)
so the data overrides the model where it's thick and defers to it where it's thin.

Growth: base + g x (n - 1) x (0.7025 + 0.0175 x (n - 1)), the community formula (unverified constants, research doc);
levels 19-20 (top's quest) extend it, which is also unverified.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

WINDOW_S = 6.0  # a trade
MAX_CASTS = 2  # an ability lands at most twice in a trade (ammo and trap abilities list tiny "cooldowns")
MIN_COOLDOWN_S = 2.0  # shorter listed cooldowns are ammo recharges or toggles: one cast
PHYSICAL_MIX = 0.6  # the reference damage mix for EHP
SHRINK_MINUTES = 400.0  # minute-observations for half weight on the measured value
DEFAULT_SLOPE = 150.0  # gold per minute per unit of relative power, before there's data to fit it
DEFAULT_ITEM_LEVELS = {1: 8, 2: 12, 3: 15}  # the level each finished item usually comes at (data/power.py measures it)


@dataclass(frozen=True)
class Milestone:
    label: str
    level: int
    items: int


def milestones(role: str, item_levels: Mapping[int, float] | None = None) -> list[Milestone]:
    """The axis: levels 2, 3, 6, item 1, 9, 11, item 2, 13, 16, item 3, 18; top adds 19 and 20 (the role quest)."""
    il = {**DEFAULT_ITEM_LEVELS, **(item_levels or {})}
    axis = [Milestone("lvl 2", 2, 0), Milestone("lvl 3", 3, 0), Milestone("lvl 6", 6, 0),
            Milestone("item 1", round(il[1]), 1), Milestone("lvl 9", 9, 1), Milestone("lvl 11", 11, 1),
            Milestone("item 2", round(il[2]), 2), Milestone("lvl 13", 13, 2), Milestone("lvl 16", 16, 2),
            Milestone("item 3", round(il[3]), 3), Milestone("lvl 18", 18, 3)]
    if role == "top":
        axis += [Milestone("lvl 19", 19, 3), Milestone("lvl 20", 20, 3)]
    return axis


def milestone_index(axis: list[Milestone], level: int, items: int) -> int:
    """The furthest milestone reached by a state: its level and its item count both met."""
    best = 0
    for i, m in enumerate(axis):
        if level >= m.level and items >= m.items:
            best = i
    return best


def growth(flat: float, per_level: float, level: int) -> float:
    n = level - 1
    return flat + per_level * n * (0.7025 + 0.0175 * n)


def ranks_at(level: int, max_order: str = "QEW") -> dict[str, int]:
    """Ability ranks at a level: R at 6 / 11 / 16; one point in each basic ability by level 3 (max order first), then
    the max order fills each to 5."""
    order = [k for k in (max_order or "QEW") if k in "QWE"] + [k for k in "QWE" if k not in (max_order or "")]
    r = sum(1 for lv in (6, 11, 16) if level >= lv)
    basic = {k: 0 for k in "QWE"}
    points = max(min(level, 18) - r, 0)
    for k in order[:3]:
        if points == 0:
            break
        basic[k] += 1
        points -= 1
    for k in order:
        add = min(5 - basic[k], points)
        basic[k] += add
        points -= add
    return {**basic, "R": r}


@dataclass(frozen=True)
class Stats:
    hp: float
    armor: float
    mr: float
    ad: float
    bonus_ad: float
    ap: float
    attack_speed: float
    crit: float


ITEM_STATS = {"FlatHPPoolMod": "hp", "FlatArmorMod": "armor", "FlatSpellBlockMod": "mr", "FlatPhysicalDamageMod": "ad",
              "FlatMagicDamageMod": "ap", "PercentAttackSpeedMod": "as", "FlatCritChanceMod": "crit"}


def item_totals(items: Iterable[Iterable[tuple[str, float]]]) -> dict[str, float]:
    out = dict.fromkeys(ITEM_STATS.values(), 0.0)
    for stats in items:
        for k, v in stats:
            if k in ITEM_STATS:
                out[ITEM_STATS[k]] += v
    return out


def champion_stats(kit, level: int, items: Iterable[Iterable[tuple[str, float]]] = ()) -> Stats:
    s, it = kit.stats, item_totals(items)
    base_ad = growth(*s["attackDamage"], level)
    as_base = s["attackSpeed"][0]
    ratio = s.get("attackSpeedRatio", (as_base, 0.0))[0] or as_base
    bonus_as = growth(0.0, s["attackSpeed"][1], level) / 100 + it["as"]
    return Stats(hp=growth(*s["health"], level) + it["hp"], armor=growth(*s["armor"], level) + it["armor"],
                 mr=growth(*s["magicResistance"], level) + it["mr"], ad=base_ad + it["ad"], bonus_ad=it["ad"],
                 ap=it["ap"], attack_speed=min(as_base + ratio * bonus_as, 2.5), crit=min(it["crit"], 1.0))


def _mitigate(kind: str, amount: float, target: Stats) -> float:
    if kind == "physical":
        return amount * 100 / (100 + max(target.armor, 0))
    if kind == "magic":
        return amount * 100 / (100 + max(target.mr, 0))
    return amount


def _part_damage(part, rank: int, me: Stats, target: Stats) -> float:
    total = 0.0
    for unit, values in part.per_rank:
        v = values[min(rank, len(values)) - 1]
        total += {"flat": v, "ad": v / 100 * me.ad, "bonus_ad": v / 100 * me.bonus_ad, "ap": v / 100 * me.ap,
                  "target_hp": v / 100 * target.hp, "own_hp": v / 100 * me.hp,
                  "target_missing_hp": v / 100 * target.hp * 0.3,  # a trade's target is ~30% down
                  "bonus_hp": 0.0}.get(unit, 0.0)
    return total


def dps(kit, me: Stats, ranks: Mapping[str, int], target: Stats, window: float = WINDOW_S) -> float:
    autos = me.attack_speed * window * me.ad * (1 + me.crit * 0.75)
    total = _mitigate("physical", autos, target)
    for key, ability in kit.abilities.items():
        rank = ranks.get(key, 0)
        if rank <= 0 or not ability.parts:
            continue
        cd = ability.cooldowns[min(rank, len(ability.cooldowns)) - 1] if ability.cooldowns else window
        casts = 1 if cd < MIN_COOLDOWN_S else min(1 + math.floor(window / cd), MAX_CASTS)
        for part in ability.parts:
            total += casts * _mitigate(part.kind, _part_damage(part, rank, me, target), target)
    return total / window


def ehp(me: Stats, physical: float = PHYSICAL_MIX) -> float:
    taken = physical * 100 / (100 + max(me.armor, 0)) + (1 - physical) * 100 / (100 + max(me.mr, 0))
    return me.hp / taken


@dataclass(frozen=True)
class Build:
    items: tuple[tuple[tuple[str, float], ...], ...]  # the first three finished items' Data Dragon stats, in order
    max_order: str = "QEW"
    modelled: bool = True  # every damaging ability read


def theory_curves(kits: Mapping[int, object], builds: Mapping[int, Build], role: str,
                  item_levels: Mapping[int, float] | None = None) -> dict[int, list[float]]:
    """champ -> relative duel power at each milestone of the role's axis (1.0 = the role's average)."""
    axis = milestones(role, item_levels)
    champs = [c for c in builds if c in kits]
    if not champs:
        return {}
    out: dict[int, list[float]] = {c: [] for c in champs}
    for m in axis:
        stats = {c: champion_stats(kits[c], m.level, builds[c].items[:m.items]) for c in champs}
        n = len(champs)
        ref = Stats(*(sum(getattr(s, f) for s in stats.values()) / n for f in Stats.__dataclass_fields__))
        dp = {c: math.sqrt(max(dps(kits[c], stats[c], ranks_at(m.level, builds[c].max_order), ref), 1e-6)
                           * ehp(stats[c])) for c in champs}
        mean = sum(dp.values()) / n
        for c in champs:
            out[c].append(dp[c] / mean)
    return out


def spikes(curves: Mapping[int, list[float]]) -> dict[int, list[int]]:
    """champ -> indices of milestones where the rise from the previous one beats the role's mean rise there by 1 SD."""
    if not curves:
        return {}
    length = min(len(v) for v in curves.values())
    out: dict[int, list[int]] = {c: [] for c in curves}
    for i in range(1, length):
        rises = {c: v[i] - v[i - 1] for c, v in curves.items()}
        mean = sum(rises.values()) / len(rises)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rises.values()) / len(rises)) or 1e-9
        for c, r in rises.items():
            if r > 0 and r - mean > sd:
                out[c].append(i)
    return out


def fit_slope(cells: Iterable[tuple[float, float, float]]) -> float:
    """(theory, measured gold/min, n) cells -> gold per minute per unit of (theory - 1), weighted least squares through 0."""
    num = den = 0.0
    for theory, measured, n in cells:
        x = theory - 1
        num += n * x * measured
        den += n * x * x
    return num / den if den > 0 and num > 0 else DEFAULT_SLOPE


def combine(theory: float | None, measured: float | None, n: float, slope: float) -> float | None:
    """Gold per minute vs the role's average: the measured value shrunk towards the theory's."""
    prior = slope * (theory - 1) if theory is not None else 0.0
    if measured is None or n <= 0:
        return prior if theory is not None else None
    w = n / (n + SHRINK_MINUTES)
    return w * measured + (1 - w) * prior


def power_summary(ours: list[dict], theirs: list[dict] | None) -> dict:
    """Plain words for a curve pair: our spikes, and where the lane swings (who's stronger from which milestone), by the
    combined value (gold per minute vs the role's average)."""
    spikes_ = [p["label"] for p in ours if p.get("spike")]
    lines = []
    if spikes_:
        lines.append("Your spikes: " + ", ".join(spikes_) + ".")
    if theirs and len(theirs) == len(ours):
        diff = [(a.get("combined") or 0) - (b.get("combined") or 0) for a, b in zip(ours, theirs)]
        phases, start = [], 0
        for i in range(1, len(diff) + 1):
            if i == len(diff) or (diff[i] > 0) != (diff[start] > 0):
                phases.append((start, i - 1, diff[start] > 0))
                start = i
        words = []
        for a, b, us in phases:
            span = ours[a]["label"] if a == b else f"{ours[a]['label']} to {ours[b]['label']}"
            words.append(f"{'you' if us else 'they'} from {span}")
        if len(phases) == 1:
            lines.append(f"{'You are' if phases[0][2] else 'They are'} stronger at every milestone.")
        else:
            lines.append("Stronger: " + "; ".join(words) + ".")
    thin = sum(1 for p in ours if (p.get("n") or 0) < SHRINK_MINUTES / 4)
    return {"lines": lines, "mostly_theory": thin > len(ours) / 2}
