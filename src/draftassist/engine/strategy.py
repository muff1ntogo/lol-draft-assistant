"""Live's Strategy view (docs/strategy.md): per locked champion of ours, its runes and build with why, and a lane plan
against its opponent; once all ten are locked, the team's win conditions and the threats to track. Pure: the builds
come in through `builds` (data/builds.py does the reading); everything else is the engine's data. Plain rules over
the measured numbers (lane gold, matchups, power curves, damage profiles) and the kit and archetype tags, so a line
only appears when its numbers or tags say so.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from draftassist.engine.comp import STYLE_LABELS, team_comp
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.state import DraftState

LANE_EVEN_GOLD = 250  # |gold at 15| under this reads as an even lane
LANE_BIG_GOLD = 600
KIT_STRONG = 0.6  # a kit tag at this intensity (0-1) or above counts
AP_HEAVY = 0.55  # enemy magic damage share that calls for magic resist
AD_HEAVY = 0.62  # physical share that calls for armour
HEALERS = 2  # enemy champions with strong sustain before anti-heal is worth it
MELEE_RANGE = 300
RANGED_ARCHETYPES = {"ranged", "artillery", "control_mage", "poke", "mage", "mage_bot"}

IDENTITY_PLANS = {
    "engage": "Group and force fights on your terms around dragon and Baron: your comp starts fights.",
    "poke": "Siege before objectives: chip them down with poke first, and don't take fights they can start.",
    "pick": "Play for vision and catches: ward the side jungle, find picks, then take objectives 5v4.",
    "protect": "Front to back: keep your carry alive and let them come to you.",
    "split": "Side lanes: send your split-pusher, hold mid with the rest, and make them answer it.",
}
VERSUS_PLANS = {
    "engage": "They want to start fights: hold vision in front of you, don't group in chokes, peel their engage.",
    "poke": "They poke: don't stand under siege; go in fast or flank before they whittle you down.",
    "pick": "They play for picks: move in groups and don't face-check unwarded brush.",
    "protect": "They protect a carry: kill the frontline fast or go around it to their carry.",
    "split": "They split-push: match the side lane with one, force objectives 4v4 elsewhere.",
}


@dataclass(frozen=True)
class BuildView:
    """What `builds(champ, role, opponent)` returns for the Strategy view: data/builds.BuildSummary plus the names."""
    summary: object  # data/builds.BuildSummary
    static: object  # data/builds.GameStatic


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{round(100 * x)}%"


def _gold(x: float) -> str:
    return f"{'+' if x > 0 else '−'}{abs(x):.0f}"


def _kit(engine, champ: int, role: str, tag: str) -> float:
    traits = engine.data.traits
    kit = getattr(traits, "kit", None) if traits is not None else None
    v = kit.intensity(champ, role, tag) if kit is not None else None
    return v or 0.0


def _range(engine, champ: int) -> float | None:
    traits = engine.data.traits
    return traits.attack_range.get(champ) if traits is not None else None


def _archetype(engine, champ: int, role: str) -> str | None:
    return (engine.data.labels or {}).get((champ, role))


def _timing(engine, champ: int, role: str) -> str | None:
    curve = engine.data.profiles.curve(champ, role)
    return curve.label if curve is not None else None


def lane_plan(engine, role: str, champ: int, opp: int | None) -> dict:
    """Who's stronger early and why, the trade pattern, and when each side spikes."""
    name = engine.data.name
    lines: list[str] = []
    if opp is None:
        return {"opponent": None, "summary": "Their laner isn't locked yet.", "lines": [], "gold15": None,
                "matchup_pp": None}
    bracket = engine.our_stats(role)
    gold = bracket.lane_gold_of(role, champ, opp)
    edge = bracket.matchup_edge(role, champ, opp)
    pp = round(25 * edge, 1)
    if gold is None:
        summary = f"No lane data vs {name(opp)} yet."
    elif abs(gold) < LANE_EVEN_GOLD:
        summary = f"Even lane vs {name(opp)} ({_gold(gold)} gold at 15 on average)."
    elif gold > 0:
        summary = (f"You're favoured early vs {name(opp)}: {_gold(gold)} gold at 15 on average"
                   + (". Press it: trade often, deny farm, look for an early kill with your jungler." if gold >= LANE_BIG_GOLD
                      else ". Take trades when their main ability is down."))
    else:
        summary = (f"{name(opp)} is favoured early: {_gold(gold)} gold at 15 on average"
                   + (". Play for farm under tower, ward for their jungler, ask for early help." if gold <= -LANE_BIG_GOLD
                      else ". Don't take extended trades early; farm safely until your items come."))
    ours_a, theirs_a = _archetype(engine, champ, role), _archetype(engine, opp, role)
    ours_r, theirs_r = _range(engine, champ), _range(engine, opp)
    melee_us = (ours_r is not None and ours_r <= MELEE_RANGE) or (ours_r is None and ours_a not in RANGED_ARCHETYPES)
    ranged_them = (theirs_r is not None and theirs_r > MELEE_RANGE) or theirs_a in RANGED_ARCHETYPES
    if melee_us and ranged_them:
        lines.append("They out-range you: give up some early CS rather than eat free poke, and all-in when their "
                     "main poke or escape is down.")
    elif not melee_us and (theirs_r is not None and theirs_r <= MELEE_RANGE):
        lines.append("You out-range them: poke on every last hit, keep your distance, punish them for walking up.")
    if _kit(engine, opp, role, "hard_gap_close") >= KIT_STRONG and _kit(engine, champ, role, "escape") < KIT_STRONG:
        lines.append(f"{name(opp)} has hard engage and you have little escape: keep the wave near your tower when "
                     "their jungler could be close.")
    if _kit(engine, opp, role, "sustain") >= KIT_STRONG:
        lines.append(f"{name(opp)} out-sustains poke: trade in short bursts, or buy an early anti-heal component.")
    if _kit(engine, champ, role, "extend_fight") >= KIT_STRONG and _kit(engine, opp, role, "extend_fight") < KIT_STRONG:
        lines.append("You win the longer trades: once you commit, stay in.")
    elif _kit(engine, opp, role, "extend_fight") >= KIT_STRONG and _kit(engine, champ, role, "extend_fight") < KIT_STRONG:
        lines.append("They win long trades: hit and step back; don't stay in extended fights.")
    if _kit(engine, opp, role, "auto_reliant") >= KIT_STRONG and _kit(engine, champ, role, "auto_denial") >= KIT_STRONG:
        lines.append("Your kit shuts down their auto attacks: trade when they commit to autos.")
    if _kit(engine, opp, role, "kite") >= KIT_STRONG and _kit(engine, champ, role, "hard_gap_close") < KIT_STRONG:
        lines.append("They kite well: don't chase without your gap-closer up.")
    t_us, t_them = _timing(engine, champ, role), _timing(engine, opp, role)
    if t_us == "late" and t_them == "early":
        lines.append("They're stronger early and you scale: survive lane, the matchup turns after your second item.")
    elif t_us == "early" and t_them == "late":
        lines.append("You're the early champion and they scale: build your lead before 20 minutes or it slips away.")
    return {"opponent": opp, "opponent_name": name(opp), "summary": summary, "lines": lines,
            "gold15": None if gold is None else round(gold), "matchup_pp": pp,
            "archetypes": [ours_a, theirs_a]}


def enemy_damage(engine, theirs: Mapping[int, str]) -> tuple[float, float] | None:
    """Their team's (physical, magic) share of damage, from the crawled damage profiles; None without data."""
    shares = [engine.data.damage.get((c, r)) for c, r in theirs.items()]
    shares = [s for s in shares if s]
    if not shares:
        return None
    phys = sum(s[0] for s in shares) / len(shares)
    magic = sum(s[1] for s in shares) / len(shares)
    return phys, magic


def _healers(engine, theirs: Mapping[int, str]) -> list[int]:
    return [c for c, r in theirs.items() if _kit(engine, c, r, "sustain") >= KIT_STRONG]


def build_plan(engine, role: str, champ: int, view: BuildView | None, theirs: Mapping[int, str],
               opp: int | None) -> dict | None:
    """Runes, spells, core items and situational items, each with its share and win rate and a reason."""
    if view is None or view.summary is None:
        return None
    s, st = view.summary, view.static
    name = engine.data.name
    keystones = [{"id": c.key, "name": st.rune_name(c.key), **c.as_dict()} for c in s.keystones]
    page = None
    if s.pages:
        import json

        runes = json.loads(s.pages[0].key)
        page = {"runes": [st.rune_name(r) for r in runes], **s.pages[0].as_dict()}
    spells = None
    if s.spells:
        import json

        spells = {"names": [st.spell_name(x) for x in json.loads(s.spells[0].key)], **s.spells[0].as_dict()}
    finished = [c for c in s.items if (it := st.items.get(c.key)) is not None and it.finished]
    if not st.items:  # names not cached: rank every item, unnamed
        finished = list(s.items)
    boots = next((c for c in s.items if (it := st.items.get(c.key)) is not None and it.boots), None)
    core = finished[:3]
    core_ids = {c.key for c in core}

    damage = enemy_damage(engine, theirs) if theirs else None
    healers = _healers(engine, theirs) if theirs else []
    situational = []
    for c in finished[3:]:
        it = st.items.get(c.key)
        if it is None:
            continue
        why = None
        if damage and damage[1] >= AP_HEAVY and "SpellBlock" in it.tags:
            why = f"their damage is {_pct(damage[1])} magic"
        elif damage and damage[0] >= AD_HEAVY and "Armor" in it.tags:
            why = f"their damage is {_pct(damage[0])} physical"
        elif len(healers) >= HEALERS and it.anti_heal:
            why = "anti-heal vs " + " and ".join(name(h) for h in healers[:3])
        if why:
            situational.append({"id": c.key, "name": it.name, "why": why, **c.as_dict()})
    if boots is not None and damage:
        it = st.items[boots.key]
        boots_why = None
        if damage[1] >= AP_HEAVY and "SpellBlock" in it.tags:
            boots_why = f"their damage is {_pct(damage[1])} magic"
        elif damage[0] >= AD_HEAVY and "Armor" in it.tags:
            boots_why = f"their damage is {_pct(damage[0])} physical"
    else:
        boots_why = None

    def item(c, why=None):
        return {"id": c.key, "name": st.item_name(c.key), "why": why, **c.as_dict()}

    reasons = []
    if keystones:
        k = keystones[0]
        reasons.append(f"{k['name']}: {_pct(k['share'])} of {s.ref_label} games"
                       + (f", {_pct(k['wr'])} win rate in your bracket ({k['games']} games)" if k["wr"] is not None else ""))
    if damage and damage[1] >= AP_HEAVY and not any(i["why"] for i in situational):
        reasons.append(f"Their damage is {_pct(damage[1])} magic: consider magic resist early.")
    elif damage and damage[0] >= AD_HEAVY and not any(i["why"] for i in situational):
        reasons.append(f"Their damage is {_pct(damage[0])} physical: armour pays off.")
    if len(healers) >= HEALERS:
        reasons.append("They heal a lot (" + ", ".join(name(h) for h in healers) + "): anti-heal is worth a slot.")
    return {
        "ref_label": s.ref_label, "ref_games": round(s.ref_games), "bracket_games": round(s.bracket_games),
        "bracket_wr": None if s.bracket_wr is None else round(s.bracket_wr, 3),
        "vs_opponent": s.opponent is not None, "opponent_name": name(opp) if s.opponent is not None and opp else None,
        "keystones": keystones, "page": page, "spells": spells,
        "core": [item(c) for c in core], "boots": item(boots, boots_why) if boots is not None else None,
        "situational": situational[:3],
        "others": [item(c) for c in finished[3:8] if c.key not in core_ids
                   and c.key not in {x["id"] for x in situational}],
        "reasons": reasons, "names_missing": not st.items,
    }


def win_conditions(engine, ours: Mapping[int, str], theirs: Mapping[int, str]) -> dict:
    """The team plan once both comps are known: power curve, what our comp wants, what to watch for in theirs."""
    profiles = engine.data.profiles
    us = team_comp(profiles, list(ours.items()))
    them = team_comp(profiles, list(theirs.items()))
    lines = []
    if us.curve is not None and them.curve is not None:
        if us.curve - them.curve > 0.1:
            lines.append("You out-scale them: trade early objectives rather than contest them at a disadvantage, "
                         "avoid coin-flip fights before your item spikes, and win the late teamfights.")
        elif them.curve - us.curve > 0.1:
            lines.append("You're the early comp: play for early dragons, grubs and herald, turn leads into towers, "
                         "and close the game before 30 minutes.")
        else:
            lines.append("Both comps peak around the same time: objectives go to whoever has the lead and vision.")
    if us.style in IDENTITY_PLANS:
        lines.append(IDENTITY_PLANS[us.style])
    if them.style in VERSUS_PLANS:
        lines.append(VERSUS_PLANS[them.style])
    if us.frontliners == 0:
        lines.append("No frontline: don't fight head-on; poke, flank or pick instead.")
    damage = enemy_damage(engine, theirs)
    if damage:
        lines.append(f"Their damage: {_pct(damage[0])} physical, {_pct(damage[1])} magic.")
    return {"ours": {"style": STYLE_LABELS.get(us.style, "Mixed"), "timing": us.timing},
            "theirs": {"style": STYLE_LABELS.get(them.style, "Mixed"), "timing": them.timing}, "lines": lines}


def strategy_view(engine, state: DraftState, our_roles: Mapping[int, str], their_roles: Mapping[int, str],
                  builds: Callable[[int, str, int | None], BuildView | None] | None, card: Callable[[int], dict | None],
                  scout_players: list[dict] | None = None) -> dict:
    """The whole Strategy payload. A player appears as soon as their champion is locked; the team plan and threats
    once all ten are."""
    their_by_role = {r: c for c, r in their_roles.items()}
    order = {r: i for i, r in enumerate(ROLES)}
    players = []
    for champ, role in sorted(our_roles.items(), key=lambda cr: order.get(cr[1], 9)):
        opp = their_by_role.get(role)
        view = builds(champ, role, opp) if builds else None
        players.append({"role": role, "role_label": ROLE_LABELS[role], "champ": card(champ),
                        "opponent": card(opp) if opp else None,
                        "lane": lane_plan(engine, role, champ, opp),
                        "build": build_plan(engine, role, champ, view, their_roles, opp)})
    complete = len(our_roles) == len(ROLES) and len(their_roles) == len(ROLES)
    team = threats = None
    if complete:
        team = win_conditions(engine, our_roles, their_roles)
        scouted = {p["role"]: p for p in (scout_players or []) if p.get("role")}
        threats = []
        for t in engine.threats(state, k=2):
            role = their_roles.get(t.champ_id)
            answer = None
            ours_there = next((c for c, r in our_roles.items() if r == role), None)
            if ours_there is not None:
                plan = lane_plan(engine, role, ours_there, t.champ_id)
                if plan["gold15"] is not None:  # "no lane data" isn't an answer
                    answer = plan["summary"]
            p = scouted.get(role)
            threats.append({"champ": card(t.champ_id), "role_label": ROLE_LABELS.get(role or "", ""), "reason": t.reason,
                            "player": p["name"] if p else None, "answer": answer})
    return {"players": players, "complete": complete, "team": team, "threats": threats}
