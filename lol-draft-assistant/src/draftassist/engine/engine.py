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


def _pct(p: float) -> str:
    return "<1%" if 0 < p < 0.005 else f"{p:.0%}"


def _pp(x: float) -> str:
    return f"{x:+.1f}".replace("-", "−") + "pp"


class DraftEngine:
    def __init__(self, data: EngineData, team: OurTeam, config: EngineConfig | None = None):
        self.data, self.team, self.cfg = data, team, config or EngineConfig()
        self._champs = sorted(data.champions())
        self._lane_data = BracketLaneData(data, team, self.cfg.enemy_bracket)
        self._role_shares = self._enemy_role_shares()
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

    def suggest_bans(self, state: DraftState, k: int = 3) -> list[Suggestion]:
        """Ban value = relevance(c) * [P(enemy picks c) * harm(c) + gamma * 0.04 * TeamLaneBan(c)].
        Champions with relevance < min_ban_relevance (roles the enemy already filled) and bans with
        value <= 0 are never suggested."""
        self._assign_cache.clear()
        lane_cfg = self.cfg.lane_ban
        ours, theirs = state.our_picks, state.their_picks
        ev = self._evaluate(state)
        base = ev.terms.total
        role_probs = self._enemy_role_probs(state, ev.their_roles)
        unavailable = state.unavailable()
        relevance = self.ban_relevance(state)
        tables = self._lane_ban_tables(state, ev)
        team_lane = team_lane_ban(tables.values())

        out = []
        for c in self._champs:
            if c in unavailable:
                continue
            rel = relevance.get(c, 0.0)
            if rel < self.cfg.min_ban_relevance:
                continue
            rp = role_probs.get(c, {})
            p = sum(rp.values())
            their_roles = self._assign_plus(THEM, tuple(theirs), c, list(ROLES))
            harm = base - self._terms(ours, ev.our_roles, theirs + [c], their_roles).total
            lift = lane_cfg.gamma * lane_cfg.pp_to_logodds * team_lane.get(c, 0.0)
            value = rel * (p * harm + lift)
            if value <= 0:
                continue
            shares = self._role_shares[c]
            likely_role = max(rp, key=rp.get) if rp else max(shares, key=shares.get)
            reason = self._ban_reason(c, p, likely_role, p * harm, lift, tables)
            hover = state.hovered_by(c)
            warnings = (f"\u26a0 hovered by {ROLE_LABELS[hover]}",) if hover else ()
            out.append(Suggestion(c, self.data.name(c), "ban", value, reason, role=likely_role, enemy_prob=p,
                                  relevance=rel, warnings=warnings))
        out.sort(key=lambda s: (-s.value, s.champ_id))
        return validate_suggestions(state, "ban", out[:k], self.cfg.min_ban_relevance)

    def _ban_reason(self, c: int, p: float, role: str, threat: float, lift: float,
                    tables: dict[str, RoleBanTable]) -> str:
        rows = [(t.role, row) for t in tables.values() if (row := t.row(c)) is not None and row.ban > 0]
        if rows and lift > max(threat, 0):
            lane_role, row = max(rows, key=lambda t: t[1].ban)
            label = ROLE_LABELS[lane_role]
            head = f"Common vs our {label if label.isupper() else label.lower()} (PR {row.pick_rate:.0%})"
            if row.via is None:
                return f"{head}; {row.via_note.strip('()')}"
            return f"{head}; our best answer {self.data.name(row.via)} only {_pp(row.resp)}"
        head = f"Their {self.data.name(c)}: {_pct(p)} likely"
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
        return validate_suggestions(state, "pick", out[:k])

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
