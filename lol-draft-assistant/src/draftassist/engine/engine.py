"""Draft engine v1: additive log-odds scoring, enemy pick model, ban and pick ranking.

Score of a (partial) draft, as log-odds that we win (spec, Draft engine):

    L = sum_A (alpha*m + beta*c) - sum_B m + sum_A pairs s - sum_B pairs s + sum_roles x

Roles are assigned per side by brute force over at most 5! permutations, maximising the
side's own meta (+ comfort for us). Pure functions over immutable inputs; no I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations, permutations

from draftassist.engine.model import (
    ROLE_LABELS,
    ROLES,
    BracketStats,
    EngineConfig,
    EngineData,
    OurTeam,
    logit,
    sigmoid,
)
from draftassist.engine.state import DraftState

US, THEM = "us", "them"
_LO_TO_PROB = 0.25  # d sigmoid / dx at 0.5: converts small log-odds terms to win-prob units


@dataclass(frozen=True)
class Terms:
    """Components of L, each already weighted and signed as it enters the sum."""
    meta_us: float = 0.0
    comfort: float = 0.0
    meta_them: float = 0.0  # subtracted
    syn_us: float = 0.0
    syn_them: float = 0.0  # subtracted
    lane: float = 0.0

    @property
    def total(self) -> float:
        return self.meta_us + self.comfort - self.meta_them + self.syn_us - self.syn_them + self.lane


@dataclass(frozen=True)
class Evaluation:
    terms: Terms
    our_roles: dict[int, str]
    their_roles: dict[int, str]

    @property
    def win_prob(self) -> float:
        return sigmoid(self.terms.total)


@dataclass(frozen=True)
class Suggestion:
    champ_id: int
    name: str
    kind: str  # ban | pick
    value: float
    reason: str
    role: str | None = None  # pick: our role for it; ban: the enemy's most likely role for it
    win_prob: float | None = None  # pick: P(win) after the pick
    enemy_prob: float | None = None  # ban: P(enemy picks it)


def _pct(p: float) -> str:
    return "<1%" if 0 < p < 0.005 else f"{p:.0%}"


class DraftEngine:
    def __init__(self, data: EngineData, team: OurTeam, config: EngineConfig | None = None):
        self.data, self.team, self.cfg = data, team, config or EngineConfig()
        self._champs = sorted(data.champions())
        self._assign_cache: dict[tuple, tuple[float, dict[int, str]]] = {}

    # ---------- lookups ----------
    def _bracket(self, name: str) -> BracketStats:
        return self.data.brackets[name]

    def _our_bracket(self, role: str) -> BracketStats:
        return self._bracket(self.team.brackets[role])

    def _enemy(self) -> BracketStats:
        return self._bracket(self.cfg.enemy_bracket)

    def _m(self, champ: int, role: str, bracket: BracketStats) -> float:
        stat = bracket.role_stat(champ, role)
        return logit(stat.win_rate if stat else self.cfg.off_role_wr)

    def _comfort(self, champ: int, role: str) -> float:
        entry = self.team.entry(champ, role)
        return entry.comfort if entry else self.cfg.off_pool_penalty

    def _weight(self, side: str, champ: int, role: str) -> float:
        if side == US:
            return self.cfg.alpha * self._m(champ, role, self._our_bracket(role)) + self.cfg.beta * self._comfort(champ, role)
        return self._m(champ, role, self._enemy())

    # ---------- role assignment ----------
    def _assign(self, side: str, champs: tuple[int, ...], roles: tuple[str, ...] = ROLES) -> tuple[float, dict[int, str]]:
        key = (side, champs, roles)
        if (hit := self._assign_cache.get(key)) is not None:
            return hit
        if len(champs) > len(roles):
            raise ValueError("more champions than roles")
        best, best_map = -math.inf, {}
        for perm in permutations(roles, len(champs)):
            s = sum(self._weight(side, c, r) for c, r in zip(champs, perm))
            if s > best:
                best, best_map = s, dict(zip(champs, perm))
        self._assign_cache[key] = (best, best_map)
        return best, best_map

    def _assign_plus(self, side: str, base: tuple[int, ...], champ: int, champ_roles: list[str]) -> dict[int, str]:
        """Best assignment of base + champ with champ restricted to champ_roles.
        Reuses the cached assignment of `base` over the remaining four roles."""
        best, best_map = -math.inf, {}
        for r in champ_roles:
            rest = tuple(x for x in ROLES if x != r)
            s, m = self._assign(side, base, rest)
            s += self._weight(side, champ, r)
            if s > best:
                best, best_map = s, {**m, champ: r}
        return best_map

    def _terms(self, ours: list[int], our_roles: dict[int, str], theirs: list[int], their_roles: dict[int, str]) -> Terms:
        cfg, enemy, team_b = self.cfg, self._enemy(), self._bracket(self.cfg.team_bracket)
        meta_us = sum(cfg.alpha * self._m(c, our_roles[c], self._our_bracket(our_roles[c])) for c in ours)
        comfort = sum(cfg.beta * self._comfort(c, our_roles[c]) for c in ours)
        meta_them = sum(self._m(c, their_roles[c], enemy) for c in theirs)
        syn_us = sum(logit(team_b.synergy_wr(a, b)) for a, b in combinations(ours, 2))
        syn_them = sum(logit(enemy.synergy_wr(a, b)) for a, b in combinations(theirs, 2))
        their_by_role = {r: c for c, r in their_roles.items()}
        lane = 0.0
        for c, r in our_roles.items():
            if (opp := their_by_role.get(r)) is not None:
                lane += logit(self._our_bracket(r).matchup_wr(r, c, opp))
        return Terms(meta_us, comfort, meta_them, syn_us, syn_them, lane)

    def _evaluate(self, state: DraftState) -> Evaluation:
        ours, theirs = state.our_picks, state.their_picks
        _, our_roles = self._assign(US, tuple(ours))
        _, their_roles = self._assign(THEM, tuple(theirs))
        return Evaluation(self._terms(ours, our_roles, theirs, their_roles), our_roles, their_roles)

    # ---------- public: evaluation ----------
    def evaluate(self, state: DraftState) -> Evaluation:
        self._assign_cache.clear()
        return self._evaluate(state)

    def win_prob(self, state: DraftState) -> float:
        return self.evaluate(state).win_prob

    # ---------- enemy model ----------
    def _enemy_role_probs(self, state: DraftState, their_roles: dict[int, str]) -> dict[int, dict[str, float]]:
        """P(enemy picks c in role r) for their next pick: uniform over their open roles, softmax over
        log(pick rate) + w * logit(win rate) within each role."""
        if state.picks_left(state.their_side, after=len(state.actions) - 1) == 0:
            return {}
        enemy, w = self._enemy(), self.cfg.enemy_strength_weight
        filled = set(their_roles.values())
        unavailable = state.unavailable()
        per_role: dict[str, dict[int, float]] = {}
        for r in ROLES:
            if r in filled:
                continue
            vals = {}
            for c in self._champs:
                stat = enemy.role_stat(c, r)
                if c not in unavailable and stat and stat.pick_rate > 0:
                    vals[c] = math.log(stat.pick_rate) + w * logit(stat.win_rate)
            if vals:
                top = max(vals.values())
                exps = {c: math.exp(v - top) for c, v in vals.items()}
                z = sum(exps.values())
                per_role[r] = {c: e / z for c, e in exps.items()}
        out: dict[int, dict[str, float]] = {}
        for r, probs in per_role.items():
            for c, p in probs.items():
                out.setdefault(c, {})[r] = p / len(per_role)
        return out

    def enemy_pick_probs(self, state: DraftState) -> dict[int, float]:
        self._assign_cache.clear()
        ev = self._evaluate(state)
        return {c: sum(rp.values()) for c, rp in self._enemy_role_probs(state, ev.their_roles).items()}

    # ---------- public: suggestions ----------
    def suggest(self, state: DraftState, k: int = 3, role: str | None = None) -> list[Suggestion]:
        step = state.step
        if step is None:
            raise ValueError("draft is complete")
        if step.side != state.our_side:
            raise ValueError("not our turn")
        return self.suggest_bans(state, k) if step.kind == "ban" else self.suggest_picks(state, k, role)

    def suggest_bans(self, state: DraftState, k: int = 3) -> list[Suggestion]:
        """Ban value = P(enemy picks c) * harm(c) + protect_weight * protect(c).
        Phase 2 bans only consider the enemy's open roles and protect our locked picks."""
        self._assign_cache.clear()
        cfg = self.cfg
        ours, theirs = state.our_picks, state.their_picks
        ev = self._evaluate(state)
        base = ev.terms.total
        role_probs = self._enemy_role_probs(state, ev.their_roles)
        phase2 = state.step is not None and state.step.phase == 2
        unavailable = state.unavailable()

        # Champions a ban could protect: our locked picks, plus (in phase 1) our pool for open roles.
        targets = list(ev.our_roles.items())
        if not phase2:
            open_roles = [r for r in ROLES if r not in ev.our_roles.values()]
            targets += [(c, r) for r in open_roles for c in sorted(self.team.pools.get(r, {})) if c not in unavailable]

        out = []
        for c in self._champs:
            if c in unavailable:
                continue
            rp = role_probs.get(c, {})
            p = sum(rp.values())
            if phase2 and p == 0:
                continue
            their_roles = self._assign_plus(THEM, tuple(theirs), c, list(ROLES))
            harm = base - self._terms(ours, ev.our_roles, theirs + [c], their_roles).total
            protect, target_wr, target = 0.0, 0.5, None
            for o, r in targets:
                wr = self._our_bracket(r).matchup_wr(r, c, o)
                if logit(wr) > protect:
                    protect, target_wr, target = logit(wr), wr, o
            value = p * harm + cfg.protect_weight * protect
            likely_role = max(rp, key=rp.get) if rp else their_roles[c]
            reason = self._ban_reason(c, p, likely_role, p * harm, cfg.protect_weight * protect, target, target_wr)
            out.append(Suggestion(c, self.data.name(c), "ban", value, reason, role=likely_role, enemy_prob=p))
        out.sort(key=lambda s: (-s.value, s.champ_id))
        return out[:k]

    def _ban_reason(self, c: int, p: float, role: str, threat: float, protect: float,
                    target: int | None, target_wr: float) -> str:
        head = f"Their {self.data.name(c)}: {_pct(p)} likely"
        if target is not None and protect > max(threat, 0):
            verb = "hard counters" if target_wr >= 0.54 else "counters"
            return f"{head}, {verb} our {self.data.name(target)}"
        stat = self._enemy().role_stat(c, role)
        return f"{head}, {stat.win_rate:.0%} WR {ROLE_LABELS[role]}" if stat else head

    def suggest_picks(self, state: DraftState, k: int = 3, role: str | None = None) -> list[Suggestion]:
        """Pick value = P(win) after the pick - risk_weight * counter risk + flex bonus."""
        self._assign_cache.clear()
        cfg = self.cfg
        ours, theirs = state.our_picks, state.their_picks
        ev = self._evaluate(state)
        open_roles = [r for r in ROLES if r not in ev.our_roles.values()]
        if role is not None:
            if role not in open_roles:
                raise ValueError(f"role {role!r} is already filled")
            roles = [role]
        else:
            roles = open_roles
        enemy = self._enemy()
        enemy_open = {r for r in ROLES if r not in ev.their_roles.values()}
        enemy_left = state.picks_left(state.their_side)
        unavailable = state.unavailable()
        enemy_cands = {r: [e for e in self._champs if e not in unavailable and enemy.role_stat(e, r)] for r in ROLES}

        out = []
        for c in self._champs:
            if c in unavailable:
                continue
            cand_roles = [r for r in roles if self.team.entry(c, r) or self._our_bracket(r).role_stat(c, r)]
            if not cand_roles:
                continue
            our_roles = self._assign_plus(US, tuple(ours), c, cand_roles)
            r_c = our_roles[c]
            win = sigmoid(self._terms(ours + [c], our_roles, theirs, ev.their_roles).total)

            risk, counter = 0.0, None
            if enemy_left and r_c in enemy_open:
                lane = self._our_bracket(r_c)
                for e in enemy_cands[r_c]:
                    if e != c and (x := logit(lane.matchup_wr(r_c, e, c))) > risk:
                        risk, counter = x, e
                risk *= min(1.0, enemy_left / cfg.risk_horizon)

            flex_roles = [r for r in open_roles if self.team.entry(c, r)]
            flex = cfg.flex_bonus * max(len(flex_roles) - 1, 0)
            value = win - cfg.risk_weight * risk + flex
            reason = self._pick_reason(c, r_c, ours, ev.their_roles, flex_roles, flex, cfg.risk_weight * risk, counter)
            out.append(Suggestion(c, self.data.name(c), "pick", value, reason, role=r_c, win_prob=win))
        out.sort(key=lambda s: (-s.value, s.champ_id))
        return out[:k]

    def _pick_reason(self, c: int, role: str, ours: list[int], their_roles: dict[int, str],
                     flex_roles: list[str], flex: float, risk: float, counter: int | None) -> str:
        """'<Role> <tier>, <largest positive term>', plus a warning if counter risk outweighs it."""
        entry = self.team.entry(c, role)
        head = f"{ROLE_LABELS[role]} {entry.tier if entry else 'off-pool'}"
        bracket_name = self.team.brackets[role]
        bracket = self._bracket(bracket_name)
        terms: list[tuple[float, str]] = []  # (contribution in win-prob units, text)

        if stat := bracket.role_stat(c, role):
            terms.append((_LO_TO_PROB * self.cfg.alpha * logit(stat.win_rate), f"{stat.win_rate:.0%} WR in {bracket_name.title()}"))
        opp = next((o for o, r in their_roles.items() if r == role), None)
        if opp is not None:
            terms.append((_LO_TO_PROB * logit(bracket.matchup_wr(role, c, opp)), f"counters their {self.data.name(opp)}"))
        team_b = self._bracket(self.cfg.team_bracket)
        for o in ours:
            terms.append((_LO_TO_PROB * logit(team_b.synergy_wr(c, o)), f"pairs with our {self.data.name(o)}"))
        if flex > 0:
            terms.append((flex, "flex " + "/".join(ROLE_LABELS[r] for r in flex_roles)))

        best = max((t for t in terms if t[0] > 0), key=lambda t: t[0], default=None)
        reason = f"{head}, {best[1]}" if best else head
        if counter is not None and risk > (best[0] if best else 0):
            reason += f"; watch for {self.data.name(counter)}"
        return reason
