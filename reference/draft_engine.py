"""
draft_engine.py — minimal LoL pick/ban recommender (prototype core).

Model: every draft state is scored as log-odds of OUR team winning:
    score = sum(meta strength) + sum(player comfort) + sum(synergy pairs)
            + sum(lane/role counter deltas) ; then  P(win) = sigmoid(score)
Bans  = remove the enemy option that costs us the most expected win prob.
Picks = add the champion that maximises win prob, penalised by how badly the
        enemy can still counter-pick it (last-pick risk) and rewarded for flex.

Replace the TOY_* data with real tables built from Riot match-v5 / Leaguepedia.
"""
from __future__ import annotations
import math
from itertools import permutations
from dataclasses import dataclass, field

ROLES = ["top", "jungle", "mid", "bot", "support"]

# Standard tournament draft: (side, action). B = blue, R = red.
DRAFT_ORDER = (
    [("B", "ban"), ("R", "ban")] * 3 +
    [("B", "pick"), ("R", "pick"), ("R", "pick"), ("B", "pick"), ("B", "pick"), ("R", "pick")] +
    [("R", "ban"), ("B", "ban")] * 2 +
    [("R", "pick"), ("B", "pick"), ("B", "pick"), ("R", "pick")]
)

def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))

def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


@dataclass
class Team:
    # player role -> {champion: comfort log-odds bonus}; built from mastery / recent games
    pool: dict[str, dict[str, float]]

    def champs(self) -> set[str]:
        return {c for r in self.pool.values() for c in r}

    def roles_for(self, champ: str) -> list[str]:
        return [r for r, cs in self.pool.items() if champ in cs]


@dataclass
class Data:
    meta: dict[str, dict[str, float]]                    # champ -> role -> patch win rate
    counters: dict[tuple[str, str], float] = field(default_factory=dict)  # (a,b) -> a's lane WR vs b
    synergy: dict[frozenset, float] = field(default_factory=dict)         # {a,b} -> duo WR

    def meta_lo(self, c, role):  return logit(self.meta.get(c, {}).get(role, 0.48))
    def counter_lo(self, a, b):  return logit(self.counters.get((a, b), 0.5))
    def synergy_lo(self, a, b):  return logit(self.synergy.get(frozenset((a, b)), 0.5))


class Draft:
    def __init__(self, data: Data, us: Team, them: Team, our_side: str = "B",
                 fearless_locked: set[str] | None = None):
        self.d, self.us, self.them, self.side = data, us, them, our_side
        self.bans: set[str] = set(fearless_locked or set())   # fearless: earlier-game champs
        self.our_picks: list[str] = []
        self.their_picks: list[str] = []

    # ---------- state ----------
    def available(self) -> set[str]:
        return set(self.d.meta) - self.bans - set(self.our_picks) - set(self.their_picks)

    def turn(self) -> tuple[str, str] | None:
        n = len(self.bans) + len(self.our_picks) + len(self.their_picks)
        return DRAFT_ORDER[n] if n < len(DRAFT_ORDER) else None

    # ---------- evaluation ----------
    def _assign(self, champs: list[str], team: Team) -> tuple[float, dict[str, str]]:
        """Best champ->role assignment (≤5! permutations, cheap)."""
        best, best_map = -1e9, {}
        for roles in permutations(ROLES, len(champs)):
            s = 0.0
            for c, r in zip(champs, roles):
                s += self.d.meta_lo(c, r) + team.pool.get(r, {}).get(c, -0.4)  # off-pool penalty
            if s > best:
                best, best_map = s, dict(zip(champs, roles))
        return best, best_map

    def score(self, ours: list[str], theirs: list[str]) -> float:
        """Log-odds that we win given (partial) comps."""
        a, amap = self._assign(ours, self.us)
        b, bmap = self._assign(theirs, self.them)
        syn = sum(self.d.synergy_lo(x, y) for i, x in enumerate(ours) for y in ours[i+1:])
        syn -= sum(self.d.synergy_lo(x, y) for i, x in enumerate(theirs) for y in theirs[i+1:])
        lane = 0.0
        for x in ours:
            for y in theirs:
                if amap.get(x) == bmap.get(y):
                    lane += self.d.counter_lo(x, y)
        return a - b + syn + lane

    def win_prob(self) -> float:
        return sigmoid(self.score(self.our_picks, self.their_picks))

    # ---------- enemy model ----------
    def enemy_pick_probs(self) -> dict[str, float]:
        """P(enemy picks c): softmax over their best value for each available champ."""
        filled = set(self._assign(self.their_picks, self.them)[1].values())
        vals = {}
        for c in self.available():
            roles = [r for r in self.them.roles_for(c) if r not in filled]
            if roles:
                vals[c] = max(self.them.pool[r][c] + self.d.meta_lo(c, r) for r in roles)
            else:  # off-pool but role still open: possible, just unlikely
                open_roles = [r for r in self.d.meta[c] if r not in filled]
                if open_roles:
                    vals[c] = max(self.d.meta_lo(c, r) for r in open_roles) - 0.4
        if not vals:
            return {}
        z = sum(math.exp(3 * v) for v in vals.values())
        return {c: math.exp(3 * v) / z for c, v in vals.items()}

    # ---------- recommendations ----------
    def suggest_bans(self, k: int = 5) -> list[tuple[str, float, str]]:
        base = self.score(self.our_picks, self.their_picks)
        probs = self.enemy_pick_probs()
        out = []
        for c in self.available():
            p = probs.get(c, 0.0)
            harm = base - self.score(self.our_picks, self.their_picks + [c])  # how much it hurts us
            # 'protect' bans: does c hard-counter something in our pool?
            protect = max((self.d.counter_lo(c, o) for o in self.us.champs()), default=0)
            value = p * harm + 0.15 * max(protect, 0)
            why = f"enemy pick p={p:.0%}, swing {harm:+.2f} lo"
            if protect > 0.2:
                why += ", counters our pool"
            out.append((c, value, why))
        return sorted(out, key=lambda t: -t[1])[:k]

    def suggest_picks(self, k: int = 5) -> list[tuple[str, float, str]]:
        filled = set(self._assign(self.our_picks, self.us)[1].values())
        picks_left_for_enemy = sum(1 for s, a in DRAFT_ORDER[self._n():]
                                   if a == "pick" and s != self.side)
        probs = self.enemy_pick_probs()
        out = []
        for c in self.available():
            roles = [r for r in self.us.roles_for(c) if r not in filled]
            if not roles:  # pool exhausted: fall back to off-pool meta picks (penalised in _assign)
                roles = [r for r in self.d.meta[c] if r not in filled]
                if not roles:
                    continue
            gain = sigmoid(self.score(self.our_picks + [c], self.their_picks))
            # counter-pick risk: best enemy answer still open, scaled by picks they have left
            risk = 0.0
            if picks_left_for_enemy:
                answers = [self.d.counter_lo(e, c) for e in probs if e != c]
                risk = max(answers, default=0) * min(1, picks_left_for_enemy / 3)
            flex = 0.02 * (len(roles) - 1)            # hides role intention
            value = gain - 0.1 * max(risk, 0) + flex
            why = f"P(win)->{gain:.1%}, counter risk {risk:+.2f}" + (", flex" if flex else "")
            out.append((c, value, why))
        return sorted(out, key=lambda t: -t[1])[:k]

    def _n(self) -> int:
        return len(self.bans) + len(self.our_picks) + len(self.their_picks)

    # ---------- apply actions ----------
    def act(self, champ: str):
        side, action = self.turn()
        if action == "ban":
            self.bans.add(champ)
        elif side == self.side:
            self.our_picks.append(champ)
        else:
            self.their_picks.append(champ)


# ------------------------------------------------------------------ demo
TOY_META = {
    "Ksante": {"top": .49}, "Rumble": {"top": .51, "mid": .50}, "Renekton": {"top": .50},
    "Gnar": {"top": .51}, "Vi": {"jungle": .51}, "Sejuani": {"jungle": .52},
    "Xin": {"jungle": .50}, "Azir": {"mid": .48}, "Orianna": {"mid": .51},
    "Taliyah": {"mid": .51, "jungle": .50}, "Syndra": {"mid": .50},
    "Kaisa": {"bot": .50}, "Varus": {"bot": .51}, "Ezreal": {"bot": .49},
    "Rell": {"support": .51}, "Nautilus": {"support": .50}, "Rakan": {"support": .51},
    "Alistar": {"support": .50},
    "Jax": {"top": .50}, "Aatrox": {"top": .49}, "Nocturne": {"jungle": .50},
    "Ahri": {"mid": .50}, "Jinx": {"bot": .51}, "Xayah": {"bot": .50},
    "Braum": {"support": .50}, "Leona": {"support": .51},
}
TOY_COUNTERS = {("Gnar", "Ksante"): .55, ("Renekton", "Gnar"): .54, ("Syndra", "Azir"): .54,
                ("Vi", "Sejuani"): .53, ("Varus", "Ezreal"): .53, ("Rell", "Rakan"): .54}
TOY_SYNERGY = {frozenset(("Kaisa", "Nautilus")): .54, frozenset(("Orianna", "Sejuani")): .55,
               frozenset(("Varus", "Alistar")): .53}

US = Team({"top": {"Ksante": .3, "Rumble": .2}, "jungle": {"Sejuani": .3, "Vi": .1},
           "mid": {"Azir": .4, "Orianna": .2, "Taliyah": .1},
           "bot": {"Kaisa": .3, "Ezreal": .2}, "support": {"Nautilus": .2, "Rakan": .3}})
THEM = Team({"top": {"Gnar": .4, "Renekton": .2}, "jungle": {"Vi": .3, "Xin": .2, "Taliyah": .1},
             "mid": {"Syndra": .3, "Orianna": .3}, "bot": {"Varus": .3, "Kaisa": .2},
             "support": {"Rell": .3, "Alistar": .2}})

if __name__ == "__main__":
    d = Draft(Data(TOY_META, TOY_COUNTERS, TOY_SYNERGY), US, THEM, our_side="B")
    while (t := d.turn()):
        side, action = t
        if side == d.side:
            recs = d.suggest_bans() if action == "ban" else d.suggest_picks()
            print(f"\n[{side} {action}] suggestions:")
            for c, v, why in recs[:3]:
                print(f"   {c:<9} {v:+.3f}  ({why})")
            if not recs:
                break
            d.act(recs[0][0])
        else:  # simulate enemy: ban our best comfort pick / pick their most likely champ
            if action == "ban":
                target = max((c for c in US.champs() if c in d.available()),
                             key=lambda c: max(US.pool[r].get(c, -9) for r in ROLES))
            else:
                probs = d.enemy_pick_probs()
                if not probs:
                    break
                target = max(probs, key=probs.get)
            print(f"[{side} {action}] enemy -> {target}")
            d.act(target)
    print(f"\nFinal: us={d.our_picks}  them={d.their_picks}  bans={sorted(d.bans)}")
    print(f"Model P(win) = {d.win_prob():.1%}")
