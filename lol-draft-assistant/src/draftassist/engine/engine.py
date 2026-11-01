"""Draft engine v1: additive log-odds scoring, enemy pick model, ban and pick ranking.

Score of a (partial) draft, as log-odds that we win (spec, Draft engine):

    L = sum_A (alpha*m + beta*c) - sum_B m + sum_A pairs s - sum_B pairs s + sum_roles x

Roles are assigned per side by brute force over at most 5! permutations, maximising the
side's own meta (+ comfort for us). Pure functions over immutable inputs; no I/O.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from itertools import combinations, permutations

from draftassist.engine.guards import validate_suggestions
from draftassist.engine.lane_ban import BracketLaneData, RoleBanTable, role_ban_table, team_lane_ban
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

log = logging.getLogger(__name__)

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
    kind: str  # ban | pick | enemy_pick
    value: float
    reason: str
    role: str | None = None  # pick: our role for it; ban / enemy_pick: the enemy's most likely role
    win_prob: float | None = None  # pick: P(win) after the pick
    enemy_prob: float | None = None  # ban / enemy_pick: P(enemy picks it next)
    relevance: float | None = None  # ban: share of its roles the enemy can still fill (API only)
    warnings: tuple[str, ...] = ()  # e.g. "\u26a0 hovered by Mid"
    counter_id: int | None = None  # pick: best enemy counter still open
    counter_risk: float = 0.0  # pick: that counter's lane edge in log-odds, scaled by picks left
    threat: float | None = None  # ban: Threat component (log-odds, unweighted)
    matchup: float | None = None  # ban: Matchup component
    comp: float | None = None  # ban: Comp component
    discount: float | None = None  # ban: 1 - P(enemy team bans it too) in blind bans, else 1


@dataclass(frozen=True)
class BanCandidate:
    champ_id: int
    threat: float
    matchup: float
    comp: float
    relevance: float
    discount: float
    value: float
    enemy_prob: float
    likely_role: str | None
    lanes: dict[str, float]  # role -> this champion's lane ban score (pp) in our tables
    comp_target: int | None  # our locked pick it counters most
    comp_wr: float


def enemy_team_ban_prob(ban_rate: float) -> float:
    """P(one given team bans X) from the per-match ban rate, assuming the two teams ban
    independently: 1 - ban_rate = (1 - q)^2, so q = 1 - sqrt(1 - ban_rate)."""
    return 1 - math.sqrt(1 - min(max(ban_rate, 0.0), 0.999999))


_ROLE_NOUNS = {"top": "top laner", "jungle": "jungler", "mid": "mid laner", "adc": "ADC", "support": "support"}


def _role_noun(role: str, possessive: bool = False) -> str:
    if possessive:
        return {"adc": "ADC's"}.get(role, f"{role}'s")
    return _ROLE_NOUNS.get(role, role)


def _with_known_brackets(team: OurTeam, data: EngineData, fallback: str) -> OurTeam:
    """A role whose bracket has no stats uses the enemy bracket (logged) instead of failing."""
    missing = {r: b for r, b in team.brackets.items() if b not in data.brackets}
    if not missing:
        return team
    if fallback not in data.brackets:
        fallback = next(iter(data.brackets))
    log.warning("no stats for bracket(s) %s; using %s for %s", sorted(set(missing.values())), fallback,
                ", ".join(sorted(missing)))
    return OurTeam(team.pools, {r: (fallback if r in missing else b) for r, b in team.brackets.items()})


def _pct(p: float) -> str:
    return "<1%" if 0 < p < 0.005 else f"{p:.0%}"


def _pp(x: float) -> str:
    return f"{x:+.1f}".replace("-", "−") + "pp"


class DraftEngine:
    def __init__(self, data: EngineData, team: OurTeam, config: EngineConfig | None = None):
        self.cfg = config or EngineConfig()
        self.data, self.team = data, _with_known_brackets(team, data, self.cfg.enemy_bracket)
        self._champs = sorted(data.champions())
        self._lane_data = BracketLaneData(data, self.team, self.cfg.enemy_bracket)
        self._role_shares = self._enemy_role_shares()
        self._assign_cache: dict[tuple, tuple[float, dict[int, str]]] = {}
        self._last_tables: dict[str, RoleBanTable] = {}

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

    def _enemy_role_shares(self) -> dict[int, dict[str, float]]:
        """P(champion is played in role) from the enemy bracket's pick rates."""
        shares = {}
        for c in self._champs:
            rates = {r: s.pick_rate for r in ROLES if (s := self._enemy().role_stat(c, r)) and s.pick_rate > 0}
            total = sum(rates.values())
            if total > 0:
                shares[c] = {r: v / total for r, v in rates.items()}
        return shares

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

    # ---------- wasted-ban filter ----------
    def role_filled_probs(self, state: DraftState) -> dict[str, float]:
        """P(enemy role r is filled), from a softmax over every assignment of their revealed picks
        scored by sum of log P(champ played in role). A 60/40 flex pick fills mid 60%, jungle 40%."""
        theirs = state.their_picks
        filled = dict.fromkeys(ROLES, 0.0)
        if not theirs:
            return filled
        floor = self.cfg.role_share_floor
        scored = []
        for perm in permutations(ROLES, len(theirs)):
            s = sum(math.log(max(self._role_shares.get(c, {}).get(r, 0.0), floor)) for c, r in zip(theirs, perm))
            scored.append((s, perm))
        top = max(s for s, _ in scored)
        weights = [(math.exp(s - top), perm) for s, perm in scored]
        z = sum(w for w, _ in weights)
        for w, perm in weights:
            for r in perm:
                filled[r] += w / z
        return filled

    def ban_relevance(self, state: DraftState) -> dict[int, float]:
        """relevance(X) = sum_r P(X played in r) * (1 - P(r filled)), for every available champion."""
        filled = self.role_filled_probs(state)
        unavailable = state.unavailable()
        return {c: sum(p * (1 - filled[r]) for r, p in self._role_shares.get(c, {}).items())
                for c in self._champs if c not in unavailable}

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

    def _lane_ban_tables(self, state: DraftState, ev: Evaluation) -> dict[str, RoleBanTable]:
        our_by_role = {r: c for c, r in ev.our_roles.items()}
        their_filled = set(ev.their_roles.values())
        unavailable = state.unavailable()
        return {
            r: role_ban_table(
                self._lane_data, r,
                {c: e.tier for c, e in self.team.pools.get(r, {}).items()},
                unavailable=unavailable,
                locked=our_by_role.get(r),
                enemy_filled=r in their_filled,
                config=self.cfg.lane_ban,
            )
            for r in ROLES
        }

    def lane_ban_tables(self, state: DraftState) -> dict[str, RoleBanTable]:
        """Per-role lane ban tables (Opp / PR / Resp / via / Ban) for the Prep page and API."""
        self._assign_cache.clear()
        return self._lane_ban_tables(state, self._evaluate(state))

    # ---------- bans ----------
    def _ban_candidates(self, state: DraftState) -> list[BanCandidate]:
        """Every ban candidate with its three components (docs/ban-value-matchups.md), unfiltered:

            ban value = relevance * discount * (a * Threat + b * Matchup + c * Comp)

        Threat  = P(enemy picks X next) * logit of X's enemy-bracket win rate in their open roles.
        Matchup = 0.04 * TeamLaneBan(X): our players' expected lane matchup gain, from their own
                  eligible pools, if X is gone. Signed: negative when our pools counter X.
        Comp    = sum over our locked picks of X's lane edge (logit) against them, weighted by the
                  chance the enemy still has that lane open. 0 before our first pick.
        relevance (ban-guardrails.md) drops bans on roles the enemy already filled; discount (Flex,
        blind simultaneous bans) is 1 - P(the enemy team bans X too)."""
        self._assign_cache.clear()
        cfg, w = self.cfg, self.cfg.ban_weights
        ev = self._evaluate(state)
        role_probs = self._enemy_role_probs(state, ev.their_roles)
        unavailable = state.unavailable()
        relevance = self.ban_relevance(state)
        filled = self.role_filled_probs(state)
        tables = self._lane_ban_tables(state, ev)
        team_lane = team_lane_ban(tables.values())
        enemy = self._enemy()
        blind = state.format.simultaneous_bans and state.step is not None and state.step.kind == "ban"
        locked = [(o, r) for o, r in ev.our_roles.items()]

        out = []
        for c in self._champs:
            if c in unavailable:
                continue
            rel = relevance.get(c, 0.0)
            if rel < cfg.min_ban_relevance:
                continue
            rp = role_probs.get(c, {})
            p = sum(rp.values())
            threat = sum(prob * logit(stat.win_rate) for r, prob in rp.items()
                         if (stat := enemy.role_stat(c, r)) is not None)
            matchup = cfg.lane_ban.pp_to_logodds * team_lane.get(c, 0.0)
            comp, comp_target, comp_wr = 0.0, None, 0.5
            for o, r in locked:
                wr = self._our_bracket(r).matchup_wr_or_none(r, c, o)
                if wr is None:
                    continue
                edge = (1 - filled[r]) * logit(wr)
                comp += edge
                if comp_target is None or edge > (1 - filled[r]) * logit(comp_wr):
                    comp_target, comp_wr = o, wr
            discount = 1 - enemy_team_ban_prob(enemy.ban_rates.get(c, 0.0)) if blind else 1.0
            weighted = w.threat * threat + w.matchup * matchup + w.comp * comp
            shares = self._role_shares.get(c, {})
            likely_role = max(rp, key=rp.get) if rp else (max(shares, key=shares.get) if shares else None)
            lanes = {t.role: row.ban for t in tables.values() if (row := t.row(c)) is not None}
            out.append(BanCandidate(c, threat, matchup, comp, rel, discount, rel * discount * weighted, p,
                                    likely_role, lanes, comp_target, comp_wr))
        out.sort(key=lambda b: (-b.value, b.champ_id))
        self._last_tables = tables
        return out

    def ban_breakdown(self, state: DraftState) -> list[dict]:
        """All candidates with their components, weighted terms and multipliers (audit, API)."""
        w = self.cfg.ban_weights
        return [{"champ": self.data.name(b.champ_id), "champ_id": b.champ_id, "Threat": b.threat,
                 "Matchup": b.matchup, "Comp": b.comp, "a*Threat": w.threat * b.threat,
                 "b*Matchup": w.matchup * b.matchup, "c*Comp": w.comp * b.comp, "rel": b.relevance,
                 "discount": b.discount, "value": b.value} for b in self._ban_candidates(state)]

    def _declared_intents(self, state: DraftState) -> frozenset[int]:
        """Standard draft: teammates' declared picks are a plan, not a passing hover: never ban them
        unless configured otherwise."""
        if state.format.simultaneous_bans and not self.cfg.ban_declared_intents:
            return frozenset(c for c, _ in state.hovers)
        return frozenset()

    def suggest_bans(self, state: DraftState, k: int = 3) -> list[Suggestion]:
        """Top-k bans with value > 0 (ban-guardrails.md Rule 3)."""
        declared = self._declared_intents(state)
        cands = [b for b in self._ban_candidates(state) if b.value > 0 and b.champ_id not in declared]
        out = [self._ban_suggestion(state, b, self._last_tables) for b in cands[:k]]
        return validate_suggestions(state, "ban", out, self.cfg.min_ban_relevance,
                                    forbidden=declared)

    def ban_group(self, state: DraftState, owners: list[str | None]) -> list[tuple[str | None, Suggestion | None]]:
        """Simultaneous bans (Flex): one coordinated ban per owner (docs/flex-simultaneous-bans.md).

        Chosen greedily with recomputation: take the best, treat it as banned, re-score (the enemy
        pick distribution and lane ban scores shift), take the next. Then each ban goes to the role
        whose lane ban score it helps most, by a one-to-one assignment maximising the total (bans
        with no lane owner fill whoever is left). Owners without a useful ban get None."""
        declared = self._declared_intents(state)
        chosen: list[tuple[Suggestion, dict[str, float]]] = []
        current = state
        for _ in range(len(owners)):
            best = next((b for b in self._ban_candidates(current) if b.value > 0 and b.champ_id not in declared), None)
            if best is None:
                break
            chosen.append((self._ban_suggestion(current, best, self._last_tables), best.lanes))
            current = current.with_provisional([best.champ_id])
        suggestions = validate_suggestions(state, "ban", [s for s, _ in chosen], self.cfg.min_ban_relevance,
                                           forbidden=declared)
        kept = [(s, lanes) for s, lanes in chosen if s in suggestions]

        best_total, best_perm = -math.inf, None
        slots = list(range(len(owners)))
        for perm in permutations(slots, len(kept)):
            total = sum(max(lanes.get(owners[slot] or "", 0.0), 0.0) for (_, lanes), slot in zip(kept, perm))
            if total > best_total + 1e-12:
                best_total, best_perm = total, perm
        assigned: dict[int, Suggestion] = {slot: s for (s, _), slot in zip(kept, best_perm or ())}
        return [(owners[i], assigned.get(i)) for i in slots]

    def _ban_suggestion(self, state: DraftState, b: BanCandidate, tables: dict[str, RoleBanTable]) -> Suggestion:
        hover = state.hovered_by(b.champ_id)
        warnings = (f"⚠ hovered by {ROLE_LABELS.get(hover, 'a teammate')}",) if hover is not None else ()
        return Suggestion(b.champ_id, self.data.name(b.champ_id), "ban", b.value, self._ban_reason(b, tables),
                          role=b.likely_role, enemy_prob=b.enemy_prob, relevance=b.relevance, warnings=warnings,
                          threat=b.threat, matchup=b.matchup, comp=b.comp, discount=b.discount)

    def _ban_reason(self, b: BanCandidate, tables: dict[str, RoleBanTable]) -> str:
        """One line from the largest positive weighted component."""
        w = self.cfg.ban_weights
        parts = {"matchup": w.matchup * b.matchup, "threat": w.threat * b.threat, "comp": w.comp * b.comp}
        top = max(parts, key=parts.get)
        if top == "matchup" and parts["matchup"] > 0 and b.lanes:
            role = max(b.lanes, key=b.lanes.get)
            row = tables[role].row(b.champ_id)
            who = f"Our {_role_noun(role, possessive=True)}"
            if row is not None and row.via is not None:
                return f"{who} best answer is {_pp(row.resp)} ({self.data.name(row.via)})"
            return f"{who} pool has no answer to it"
        if top == "comp" and parts["comp"] > 0 and b.comp_target is not None:
            verb = "Hard counters" if b.comp_wr >= 0.54 else "Counters"
            return f"{verb} our locked {self.data.name(b.comp_target)}"
        role = b.likely_role
        if role is None:
            return f"{_pct(b.enemy_prob)} likely"
        rank = self._meta_rank(b.champ_id, role)
        tier = next((f"Top-{n} meta" for n in (3, 5, 10) if rank <= n), "Meta")
        return f"{tier} {_role_noun(role)}, {_pct(b.enemy_prob)} likely"

    def _meta_rank(self, champ: int, role: str) -> int:
        enemy = self._enemy()
        rates = sorted(((s.pick_rate, c) for c in self._champs if (s := enemy.role_stat(c, role))), reverse=True)
        return next((i + 1 for i, (_, c) in enumerate(rates) if c == champ), len(rates) + 1)

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
            out.append(Suggestion(c, self.data.name(c), "pick", value, reason, role=r_c, win_prob=win,
                                  counter_id=counter, counter_risk=risk))
        out.sort(key=lambda s: (-s.value, s.champ_id))
        return validate_suggestions(state, "pick", out[:k])

    def threats(self, state: DraftState, k: int = 2) -> list[Suggestion]:
        """The enemy's biggest threats among their locked picks: meta strength in their role, plus
        their lane edge over our champion there, plus half their duo synergy. Not suggestions, so
        not guarded (they are picked champions by definition)."""
        self._assign_cache.clear()
        ev = self._evaluate(state)
        enemy = self._enemy()
        our_by_role = {r: c for c, r in ev.our_roles.items()}
        out = []
        for c, r in ev.their_roles.items():
            meta = self._m(c, r, enemy)
            opp = our_by_role.get(r)
            lane_wr = 1 - self._our_bracket(r).matchup_wr(r, opp, c) if opp is not None else 0.5
            lane = logit(lane_wr)
            partners = [(logit(enemy.synergy_wr(c, o)), o) for o in ev.their_roles if o != c]
            best_syn = max(partners, default=(0.0, None))
            syn = sum(s for s, _ in partners) / 2
            if opp is not None and lane > max(meta, best_syn[0], 0):
                reason = f"{ROLE_LABELS[r]}: {lane_wr:.0%} lane vs our {self.data.name(opp)}"
            elif best_syn[1] is not None and best_syn[0] > max(meta, 0):
                reason = f"{ROLE_LABELS[r]}: pairs with their {self.data.name(best_syn[1])}"
            else:
                stat = enemy.role_stat(c, r)
                wr = stat.win_rate if stat else self.cfg.off_role_wr
                reason = f"{ROLE_LABELS[r]}: {wr:.0%} WR in {self.cfg.enemy_bracket.title()}"
            out.append(Suggestion(c, self.data.name(c), "threat", meta + lane + syn, reason, role=r))
        out.sort(key=lambda s: (-s.value, s.champ_id))
        return out[:k]

    def likely_enemy_picks(self, state: DraftState, k: int = 3) -> list[Suggestion]:
        """The enemy's most likely next picks (shown on their turn)."""
        self._assign_cache.clear()
        ev = self._evaluate(state)
        out = []
        for c, rp in self._enemy_role_probs(state, ev.their_roles).items():
            p = sum(rp.values())
            role = max(rp, key=rp.get)
            out.append(Suggestion(c, self.data.name(c), "enemy_pick", p, f"{_pct(p)} likely, {ROLE_LABELS[role]}",
                                  role=role, enemy_prob=p))
        out.sort(key=lambda s: (-s.value, s.champ_id))
        return validate_suggestions(state, "enemy_pick", out[:k])

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
