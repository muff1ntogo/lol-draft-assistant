"""Draft engine v1: additive log-odds scoring, enemy pick model, ban and pick ranking.

Score of a (partial) draft, as log-odds that we win (spec, Draft engine):

    L = sum_A (alpha*m + beta*c) - sum_B m + sum_A pairs s - sum_B pairs s + sum_roles x + d

s is the role-aware synergy score (engine/synergy.py) of each pair in its assigned roles; d is the
damage balance penalty when our comp is almost all one damage type.

Roles are assigned per side by brute force over at most 5! permutations: ours maximise meta + comfort,
theirs are the most likely assignment from how often each champion is played in each role. Pure functions over immutable inputs; no I/O.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from itertools import combinations, permutations

from draftassist.engine.comp import STYLE_LABELS, STYLE_SHARE, STYLES, team_comp
from draftassist.engine.guards import validate_suggestions
from draftassist.engine.bot_lane import BotLaneData
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
from draftassist.engine.synergy import to_pp

log = logging.getLogger(__name__)

US, THEM = "us", "them"
_PARTNER_TIER_WEIGHT = {"main": 3.0, "comfortable": 2.0, "learning": 1.0}
_LANE_MENTION_GOLD = 300  # an expected lead this large is always mentioned (user, 4 Nov 2026)
_PAIR_MENTION_PP = 0.5  # a bot pairing worth at least this gets a reason line
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
    damage: float = 0.0  # damage balance penalty on our comp (<= 0); 0 with synergy v2 (Team covers it)
    team: float = 0.0  # synergy v2: w_team x Team vs enemy (docs/synergy-v2.md), 0 without it

    @property
    def total(self) -> float:
        return (self.meta_us + self.comfort - self.meta_them + self.syn_us - self.syn_them + self.lane + self.damage
                + self.team)


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
    own_loss: float | None = None  # ban: what banning our own pool champion costs us (log-odds)
    discount: float | None = None  # ban: 1 - P(enemy team bans it too) in blind bans, else 1
    comp_fit: float = 0.0  # pick: comp coherence term (log-odds, damped), included in value but not win_prob
    plan: bool = False  # a pinned plan's bonus applied (docs/synergy-v2.md, Stage 6)


@dataclass(frozen=True)
class PickPair:
    """Two picks in a row (pick-ahead): both champions, P(win) with both, and their pair synergy."""
    first: Suggestion
    second: Suggestion
    win_prob: float
    synergy_pp: float
    low_data: bool


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
    own_loss: float = 0.0
    own_role: str | None = None  # the role of ours that would lose it
    comp_synergy: float = 0.0  # part of comp from synergy with their locked picks (ban_comp_synergy)
    comp_partner: int | None = None  # their locked pick it pairs with best


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
        self._lane_data = BracketLaneData(data, self.team, self.cfg.enemy_bracket, self.cfg.lane_weight)
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
        fit = self._weight if side == US else self._role_likelihood
        best, best_map = -math.inf, {}
        for perm in permutations(roles, len(champs)):
            s = sum(fit(side, c, r) for c, r in zip(champs, perm))
            if s > best:
                best, best_map = s, dict(zip(champs, perm))
        self._assign_cache[key] = (best, best_map)
        return best, best_map

    def likely_roles(self, champs: list[int], side: str = THEM, roles: tuple[str, ...] = ROLES) -> dict[int, str]:
        """Role assignment for a set of champions over `roles`: the enemy's most likely one (THEM, default) or
        our best one (US). Empty when there are more champions than roles."""
        if not champs or len(champs) > len(roles):
            return {}
        return dict(self._assign(side, tuple(champs), tuple(roles))[1])

    # ---------- read-only views for planners (engine/plan.py) ----------
    @property
    def champion_ids(self) -> list[int]:
        return self._champs

    @property
    def enemy_stats(self) -> BracketStats:
        return self._enemy()

    def our_stats(self, role: str) -> BracketStats:
        """The stats of the bracket our player in `role` is scored in."""
        return self._our_bracket(role)

    def role_shares(self, champ: int) -> dict[str, float]:
        """P(champion is played in role) in the enemy bracket."""
        return dict(self._role_shares.get(champ, {}))

    def pick_weight(self, champ: int, role: str) -> float:
        """Our pick weight: alpha * meta + beta * comfort, log-odds."""
        return self._weight(US, champ, role)

    def ban_suggestions(self, state: DraftState, plan: dict[str, int] | None = None,
                        extra: dict[int, float] | None = None) -> list[Suggestion]:
        """Every ban candidate with value > 0 as a suggestion, best first, before the guardrails (callers
        filter and validate). `plan` / `extra` as in `_ban_candidates`."""
        cands = self._ban_candidates(state, plan, extra)
        return [self._ban_suggestion(state, b, self._last_tables) for b in cands if b.value > 0]

    def _role_likelihood(self, side: str, champ: int, role: str) -> float:
        """Enemy roles: log P(champion played in role) from pick rates, so the assignment is the most likely
        one, not the one with their best win rates (a jungler with a high WR in rare top games stays the
        jungler). Meta strength only breaks ties (e.g. champions with no pick data)."""
        share = self._role_shares.get(champ, {}).get(role, 0.0)
        return math.log(max(share, self.cfg.role_share_floor)) + 1e-3 * self._weight(side, champ, role)

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

    def _terms(self, ours: list[int], our_roles: dict[int, str], theirs: list[int], their_roles: dict[int, str],
               syn: tuple[float, float, float] | None = None) -> Terms:
        """`syn` = (syn_us, syn_them, team) already computed (batched candidates), else computed here."""
        cfg, enemy = self.cfg, self._enemy()
        meta_us = sum(cfg.alpha * self._m(c, our_roles[c], self._our_bracket(our_roles[c])) for c in ours)
        comfort = sum(cfg.beta * self._comfort(c, our_roles[c]) for c in ours)
        meta_them = sum(self._m(c, their_roles[c], enemy) for c in theirs)
        their_by_role = {r: c for c, r in their_roles.items()}
        lane = 0.0
        for c, r in our_roles.items():
            if (opp := their_by_role.get(r)) is not None:
                lane += self.role_weight(r) * self._our_bracket(r).matchup_edge(r, c, opp)
        if (bot := self.data.bot) is not None:  # 2v2: cross terms only; the pairing is in synergy
            our_by_role = {r: c for c, r in our_roles.items()}
            cross = bot.game_cross(our_by_role.get("adc"), our_by_role.get("support"),
                                   their_by_role.get("adc"), their_by_role.get("support"))
            lane += cross * (self.role_weight("adc") + self.role_weight("support")) / 2
        if self.data.synergy_v2 is not None:
            syn_us, syn_them, team = syn if syn is not None else self._syn_v2(ours, our_roles, theirs, their_roles)
            return Terms(meta_us, comfort, meta_them, syn_us, syn_them, lane, 0.0, team)
        syn_us = self._syn(ours, our_roles)
        syn_them = self._syn(theirs, their_roles)
        return Terms(meta_us, comfort, meta_them, syn_us, syn_them, lane, self._damage_term(ours, our_roles))

    # ---------- synergy v2 (docs/synergy-v2.md): Stat + Style per pair, Team vs enemy ----------
    def _v2_pairs(self, champs: list[int], roles: dict[int, str]) -> float:
        """Sum of each pair's part of Overall (Stat, and Style for the core in-lane pairs)."""
        ev = self.data.synergy_v2
        return sum(ev.pair(roles[a], a, roles[b], b).contribution for a, b in combinations(champs, 2))

    def _syn_v2(self, ours: list[int], our_roles: dict[int, str], theirs: list[int],
                their_roles: dict[int, str]) -> tuple[float, float, float]:
        ev = self.data.synergy_v2
        team = 0.0
        if ours or theirs:
            team = ev.weights.team * ev.team.team_vs([(c, our_roles[c]) for c in ours],
                                                     [(c, their_roles[c]) for c in theirs], fill=True).total
        return self._v2_pairs(ours, our_roles), self._v2_pairs(theirs, their_roles), team

    def _syn_v2_batch(self, comps: list[tuple[list[int], dict[int, str]]], theirs: list[int],
                      their_roles: dict[int, str]) -> list[tuple[float, float, float]]:
        """Many candidate comps of ours against one enemy comp: Team vs enemy in one numpy batch."""
        ev = self.data.synergy_v2
        syn_them = self._v2_pairs(theirs, their_roles)
        enemy = [(c, their_roles[c]) for c in theirs]
        teams = ev.team.many_vs([[(c, roles[c]) for c in champs] for champs, roles in comps], enemy, fill=True)
        return [(self._v2_pairs(champs, roles), syn_them, ev.weights.team * float(t))
                for (champs, roles), t in zip(comps, teams)]

    def pair_synergy(self, role_a: str, a: int, role_b: str, b: int) -> tuple[float, bool]:
        """A pair's synergy in log-odds for reasons and pick pairs: its part of Overall with synergy v2, else
        the v1 Stat score; and whether it is low data."""
        if self.data.synergy_v2 is not None:
            p = self.data.synergy_v2.pair(role_a, a, role_b, b)
            return p.contribution, p.low_data
        p = self.data.synergy.pair(role_a, a, role_b, b)
        return p.s, p.low_data

    def _syn(self, champs: list[int], roles: dict[int, str]) -> float:
        """Sum of role-aware pair synergy over every pair, each champion in its assigned role."""
        table = self.data.synergy
        return sum(table.pair(roles[a], a, roles[b], b).s for a, b in combinations(champs, 2))

    def damage_shares(self, champs: list[int], roles: dict[int, str]) -> tuple[tuple[float, float, float], int] | None:
        """Mean physical / magic / true share over the champions with damage data, and how many had it."""
        known = [d for c in champs if (d := self.data.damage.get((c, roles[c]))) is not None]
        if not known:
            return None
        return tuple(sum(d[i] for d in known) / len(known) for i in range(3)), len(known)

    def _damage_term(self, ours: list[int], roles: dict[int, str]) -> float:
        cfg = self.cfg
        if not cfg.damage_balance or len(ours) < cfg.damage_min_champs:
            return 0.0
        shares = self.damage_shares(ours, roles)
        if shares is None or shares[1] < cfg.damage_min_champs:
            return 0.0
        return cfg.damage_penalty if max(shares[0]) > cfg.damage_threshold else 0.0

    # ---------- comp coherence (docs/comp-style.md Stage C) ----------
    def target_for(self, ours: list[tuple[int, str | None]]) -> tuple[str | None, str | None]:
        """(style to pull towards, where it comes from: "team" | "comp"). The Team page's style wins;
        "Mixed" set there means no pull. Otherwise the style our comp is named, or leaning, towards."""
        cfg = self.cfg
        if not cfg.comp_coherence:
            return None, None
        if cfg.comp_target is not None:
            return (cfg.comp_target if cfg.comp_target in STYLES else None), "team"
        if not ours:
            return None, None
        tc = team_comp(self.data.profiles, ours)
        if tc.style != "mixed":
            return tc.style, "comp"
        top = max(STYLES, key=lambda k: tc.shares[k])
        return (top, "comp") if tc.shares[top] >= STYLE_SHARE else (None, None)

    def comp_target(self, state: DraftState) -> tuple[str | None, str | None]:
        self._assign_cache.clear()
        ev = self._evaluate(state)
        return self.target_for([(c, ev.our_roles.get(c)) for c in state.our_picks])

    def coherence(self, ours: list[tuple[int, str | None]], champ: int, role: str | None,
                  target: str | None) -> float:
        """weight * damping(our pick number) * (champ's share of the target style - 1/5), in log-odds.
        1/5 is an even spread over the five identities, so a neutral champion scores 0 and an off-style one
        a little below."""
        cfg = self.cfg
        damp = cfg.comp_damping[min(len(ours), len(cfg.comp_damping) - 1)]
        st = self.data.profiles.style(champ, role)
        if target is None or damp == 0 or st is None:
            return 0.0
        fit = st.weights[STYLES.index(target)]
        return cfg.comp_weight * damp * (fit - 1 / len(STYLES))

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

    def _lane_ban_tables(self, state: DraftState, ev: Evaluation,
                         plan: dict[str, int] | None = None) -> dict[str, RoleBanTable]:
        """`plan` (Prep page, docs/synergy-v2.md): our planned champion per role, treated as locked there."""
        our_by_role = {r: c for c, r in ev.our_roles.items()}
        for r, c in (plan or {}).items():
            our_by_role.setdefault(r, c)
        their_filled = set(ev.their_roles.values())
        unavailable = state.unavailable()
        data = self._lane_data
        if self.data.bot is not None:  # 2v2: our partner (locked, else expected from the pool) and the enemy's
            partners = {r: self._expected_partners(r, our_by_role.get(r), unavailable) for r in ("adc", "support")}
            enemy = {r: c for c, r in ev.their_roles.items()}
            data = BotLaneData(data, self.data.bot, partners, enemy, self.cfg.lane_weight)
        return {
            r: role_ban_table(
                data, r,
                {c: e.tier for c, e in self.team.pools.get(r, {}).items()},
                unavailable=unavailable,
                locked=our_by_role.get(r),
                enemy_filled=r in their_filled,
                config=self.cfg.lane_ban,
            )
            for r in ROLES
        }

    def _expected_partners(self, role: str, locked: int | None, unavailable) -> list[tuple[int, float]]:
        """Who plays `role` on our team: the locked champion, else the pool weighted by tier."""
        if locked is not None:
            return [(locked, 1.0)]
        pool = [(c, _PARTNER_TIER_WEIGHT[e.tier]) for c, e in self.team.pools.get(role, {}).items() if c not in unavailable]
        total = sum(w for _, w in pool)
        return [(c, w / total) for c, w in pool] if total else []

    def lane_ban_tables(self, state: DraftState, plan: dict[str, int] | None = None) -> dict[str, RoleBanTable]:
        """Per-role lane ban tables (Opp / PR / Resp / via / Ban) for the Prep page and API; `plan` treats our
        planned champions as locked in their roles."""
        self._assign_cache.clear()
        return self._lane_ban_tables(state, self._evaluate(state), plan)

    # ---------- bans ----------
    def _ban_candidates(self, state: DraftState, plan: dict[str, int] | None = None,
                        extra: dict[int, float] | None = None) -> list[BanCandidate]:
        """Every ban candidate with its three components (docs/ban-value-matchups.md), unfiltered:

            ban value = relevance * discount * (a * Threat + b * Matchup + c * Comp)

        Threat  = P(enemy picks X next) * logit of X's enemy-bracket win rate in their open roles.
        Matchup = 0.04 * TeamLaneBan(X): our players' expected lane matchup gain, from their own
                  eligible pools, if X is gone. Signed: negative when our pools counter X.
        Comp    = sum over our locked picks of X's lane edge (logit) against them, weighted by the
                  chance the enemy still has that lane open. 0 before our first pick.
        relevance (ban-guardrails.md) drops bans on roles the enemy already filled; discount (Flex,
        blind simultaneous bans) is 1 - P(the enemy team bans X too).

        OwnLoss = for one of our own pool champions, in a role we haven't locked yet: how much better
                  it is than that player's best other available option (alpha * meta + beta * comfort,
                  log-odds); 0 if it isn't their best. Subtracted with weight `own`, outside the
                  enemy-side multipliers: banning our own pick costs us whatever the enemy does.

        Prep plans (docs/synergy-v2.md, Stage 5): `plan` treats our planned champions as locked in their
        roles (Matchup responders, Comp targets, no self-ban check there); `extra` (log-odds per champion)
        is added inside the weighted sum, e.g. d x Win teamfights."""
        self._assign_cache.clear()
        cfg, w = self.cfg, self.cfg.ban_weights
        ev = self._evaluate(state)
        role_probs = self._enemy_role_probs(state, ev.their_roles)
        unavailable = state.unavailable()
        relevance = self.ban_relevance(state)
        filled = self.role_filled_probs(state)
        tables = self._lane_ban_tables(state, ev, plan)
        team_lane = team_lane_ban(tables.values(), cfg.role_weights)
        enemy = self._enemy()
        blind = state.format.simultaneous_bans and state.step is not None and state.step.kind == "ban"
        locked = [(o, r) for o, r in ev.our_roles.items()]
        locked += [(c, r) for r, c in (plan or {}).items()
                   if r not in ev.our_roles.values() and c not in ev.our_roles and c not in unavailable]
        own_best = self._own_best_two(state, ev, set(plan or ()))
        syn_on = cfg.ban_comp_synergy and state.step is not None and state.step.phase == 2 and bool(ev.their_roles)

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
            comp, comp_target, comp_wr, comp_best = 0.0, None, 0.5, 0.0
            for o, r in locked:
                wr = self._our_bracket(r).matchup_wr_or_none(r, c, o)
                if wr is None:
                    continue
                edge = (1 - filled[r]) * self._our_bracket(r).matchup_edge(r, c, o)
                comp += edge
                if comp_target is None or edge > comp_best:
                    comp_target, comp_wr, comp_best = o, wr, edge
            comp_syn, comp_partner = 0.0, None
            if syn_on and p > 0:
                # synergy X would add to their locked picks, in X's likely roles
                best_pair = 0.0
                for r, prob in rp.items():
                    for j, rj in ev.their_roles.items():
                        part = prob / p * self.data.synergy.pair(r, c, rj, j).s
                        comp_syn += part
                        if part > best_pair:
                            best_pair, comp_partner = part, j
                comp += comp_syn
            discount = 1 - enemy_team_ban_prob(enemy.ban_rates.get(c, 0.0)) if blind else 1.0
            weighted = w.threat * threat + w.matchup * matchup + w.comp * comp + (extra or {}).get(c, 0.0)
            own_loss, own_role = 0.0, None
            for r, best in own_best.items():
                if c in self.team.pools.get(r, {}) and best and best[0][1] == c:
                    loss = best[0][0] - (best[1][0] if len(best) > 1 else best[0][0])
                    if loss > own_loss:
                        own_loss, own_role = loss, r
            shares = self._role_shares.get(c, {})
            likely_role = max(rp, key=rp.get) if rp else (max(shares, key=shares.get) if shares else None)
            lanes = {t.role: row.ban for t in tables.values() if (row := t.row(c)) is not None}
            out.append(BanCandidate(c, threat, matchup, comp, rel, discount,
                                    rel * discount * weighted - w.own * own_loss, p,
                                    likely_role, lanes, comp_target, comp_wr, own_loss, own_role,
                                    comp_syn, comp_partner))
        out.sort(key=lambda b: (-b.value, b.champ_id))
        self._last_tables = tables
        return out

    def _own_best_two(self, state: DraftState, ev: Evaluation,
                      planned: set[str] = frozenset()) -> dict[str, list[tuple[float, int]]]:
        """Per role we haven't locked: our two best available options by pick weight (pool champions,
        plus the best off-pool champion as a fallback). Used for the self-ban penalty."""
        unavailable = state.unavailable()
        out = {}
        for r in ROLES:
            if r in ev.our_roles.values() or r in planned:
                continue
            pool = [c for c in self.team.pools.get(r, {}) if c not in unavailable]
            if not pool:
                continue
            bracket = self._our_bracket(r)
            off_pool = max((self._weight(US, c, r) for c in self._champs
                            if c not in unavailable and c not in self.team.pools.get(r, {}) and bracket.role_stat(c, r)),
                           default=None)
            ranked = sorted(((self._weight(US, c, r), c) for c in pool), key=lambda t: (-t[0], t[1]))
            if off_pool is not None:
                ranked = sorted(ranked + [(off_pool, -1)], key=lambda t: (-t[0], t[1]))
            out[r] = ranked[:2]
        return out

    def ban_breakdown(self, state: DraftState) -> list[dict]:
        """All candidates with their components, weighted terms and multipliers (audit, API)."""
        w = self.cfg.ban_weights
        return [{"champ": self.data.name(b.champ_id), "champ_id": b.champ_id, "Threat": b.threat,
                 "Matchup": b.matchup, "Comp": b.comp, "a*Threat": w.threat * b.threat,
                 "b*Matchup": w.matchup * b.matchup, "c*Comp": w.comp * b.comp, "OwnLoss": b.own_loss,
                 "d*OwnLoss": w.own * b.own_loss, "rel": b.relevance,
                 "discount": b.discount, "value": b.value} for b in self._ban_candidates(state)]

    def _declared_intents(self, state: DraftState) -> frozenset[int]:
        """Standard draft: teammates' declared picks are a plan, not a passing hover: never ban them
        unless configured otherwise."""
        if state.format.simultaneous_bans and not self.cfg.ban_declared_intents:
            return frozenset(c for c, _ in state.hovers)
        return frozenset()

    @staticmethod
    def _with_bonus(cands: list[BanCandidate], bonus: dict[int, float] | None) -> list[BanCandidate]:
        """A pinned plan's ban bonus (log-odds) on its bans, only where the ban already has value > 0: a ban
        that hurts us doesn't become a suggestion because a plan listed it."""
        if not bonus:
            return cands
        from dataclasses import replace as _replace

        out = [_replace(b, value=b.value + bonus[b.champ_id]) if b.champ_id in bonus and b.value > 0 else b
               for b in cands]
        out.sort(key=lambda b: (-b.value, b.champ_id))
        return out

    def suggest_bans(self, state: DraftState, k: int = 3, bonus: dict[int, float] | None = None) -> list[Suggestion]:
        """Top-k bans with value > 0 (ban-guardrails.md Rule 3). `bonus`: a pinned plan's ban bonus."""
        declared = self._declared_intents(state)
        cands = [b for b in self._with_bonus(self._ban_candidates(state), bonus)
                 if b.value > 0 and b.champ_id not in declared]
        out = [self._ban_suggestion(state, b, self._last_tables, plan=b.champ_id in (bonus or {}))
               for b in cands[:k]]
        return validate_suggestions(state, "ban", out, self.cfg.min_ban_relevance,
                                    forbidden=declared)

    def ban_group(self, state: DraftState, owners: list[str | None], plan: dict[str, int] | None = None,
                  extra: dict[int, float] | None = None, exclude: frozenset[int] = frozenset(),
                  bonus: dict[int, float] | None = None) -> list[tuple[str | None, Suggestion | None]]:
        """Simultaneous bans (Flex): one coordinated ban per owner (docs/flex-simultaneous-bans.md).

        Chosen greedily with recomputation: take the best, treat it as banned, re-score (the enemy
        pick distribution and lane ban scores shift), take the next. Then each ban goes to the role
        whose lane ban score it helps most, by a one-to-one assignment maximising the total (bans
        with no lane owner fill whoever is left). Owners without a useful ban get None."""
        declared = self._declared_intents(state) | exclude
        chosen: list[tuple[Suggestion, dict[str, float]]] = []
        current = state
        for _ in range(len(owners)):
            best = next((b for b in self._with_bonus(self._ban_candidates(current, plan, extra), bonus)
                         if b.value > 0 and b.champ_id not in declared), None)
            if best is None:
                break
            chosen.append((self._ban_suggestion(current, best, self._last_tables,
                                                plan=best.champ_id in (bonus or {})), best.lanes))
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

    def _ban_suggestion(self, state: DraftState, b: BanCandidate, tables: dict[str, RoleBanTable],
                        plan: bool = False) -> Suggestion:
        hover = state.hovered_by(b.champ_id)
        warnings = (f"⚠ hovered by {ROLE_LABELS.get(hover, 'a teammate')}",) if hover is not None else ()
        if b.own_loss > 0 and b.own_role:
            warnings += (f"⚠ our {_role_noun(b.own_role, possessive=True)} best pick",)
        return Suggestion(b.champ_id, self.data.name(b.champ_id), "ban", b.value, self._ban_reason(b, tables),
                          role=b.likely_role, enemy_prob=b.enemy_prob, relevance=b.relevance, warnings=warnings,
                          threat=b.threat, matchup=b.matchup, comp=b.comp, discount=b.discount, own_loss=b.own_loss,
                          plan=plan)

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
        if (top == "comp" and parts["comp"] > 0 and b.comp_partner is not None
                and b.comp_synergy > b.comp - b.comp_synergy):
            return f"Strong synergy with their {self.data.name(b.comp_partner)}"
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

    def suggest_picks(self, state: DraftState, k: int = 3, role: str | None = None,
                      bonus: dict[int, float] | None = None) -> list[Suggestion]:
        """Pick value = P(win) after the pick - risk_weight * counter risk + flex bonus.
        `bonus`: a pinned plan's pick bonus (log-odds, converted like comp coherence) for the plan champions
        due at this slot; they still pass every guardrail."""
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
        locked = [(c, ev.our_roles.get(c)) for c in ours]
        target, _ = self.target_for(locked)

        cands = []
        for c in self._champs:
            if c in unavailable:
                continue
            cand_roles = [r for r in roles if self.team.entry(c, r) or self._our_bracket(r).role_stat(c, r)]
            if cand_roles:
                cands.append((c, self._assign_plus(US, tuple(ours), c, cand_roles)))
        syns: list = [None] * len(cands)
        if self.data.synergy_v2 is not None and cands:  # Team vs enemy for every candidate in one batch
            syns = self._syn_v2_batch([(ours + [c], roles_) for c, roles_ in cands], theirs, ev.their_roles)

        out = []
        for (c, our_roles), syn in zip(cands, syns):
            r_c = our_roles[c]
            win = sigmoid(self._terms(ours + [c], our_roles, theirs, ev.their_roles, syn).total)

            risk, counter = 0.0, None
            if enemy_left and r_c in enemy_open:
                lane = self._our_bracket(r_c)
                for e in enemy_cands[r_c]:
                    if e != c and (x := lane.counter_term(r_c, e, c, cfg.lane_weight)) > risk:
                        risk, counter = x, e
                risk *= min(1.0, enemy_left / cfg.risk_horizon) * self.role_weight(r_c)

            flex_roles = [r for r in open_roles if self.team.entry(c, r)]
            flex = cfg.flex_bonus * max(len(flex_roles) - 1, 0)
            fit = self.coherence(locked, c, r_c, target)
            value = win - cfg.risk_weight * risk + flex + _LO_TO_PROB * fit
            in_plan = bool(bonus) and c in bonus
            if in_plan:
                value += _LO_TO_PROB * bonus[c]
            reason = self._pick_reason(c, r_c, our_roles, ev.their_roles, flex_roles, flex, cfg.risk_weight * risk, counter,
                                       (_LO_TO_PROB * fit, target))
            out.append(Suggestion(c, self.data.name(c), "pick", value, reason, role=r_c, win_prob=win,
                                  counter_id=counter, counter_risk=risk, comp_fit=fit, plan=in_plan))
        out.sort(key=lambda s: (-s.value, s.champ_id))
        return validate_suggestions(state, "pick", out[:k])

    # ---------- pick-ahead ----------
    @staticmethod
    def back_to_back(state: DraftState) -> bool:
        """Our pick now and our pick next (e.g. B2/B3, R1/R2, B4/B5 in tournament draft)."""
        step = state.step
        if step is None or step.kind != "pick" or step.side != state.our_side:
            return False
        steps = state.format.steps
        nxt = steps[step.index + 1] if step.index + 1 < len(steps) else None
        return nxt is not None and nxt.kind == "pick" and nxt.side == state.our_side

    def suggest_pick_pairs(self, state: DraftState, k: int = 3, per_role: int = 6) -> list[PickPair]:
        """Top-k pairs for two picks in a row, from our open roles' pools, ranked by the engine score
        with both picks (synergy included) plus comp coherence. Each role contributes its `per_role`
        best single picks.

        Varied: when choosing the next pair, each time one of its champions was already shown costs it
        `pair_repeat_margin`, so a champion repeats only when the call for it really is that strong."""
        if not self.back_to_back(state):
            return []
        self._assign_cache.clear()
        ours, theirs = state.our_picks, state.their_picks
        ev = self._evaluate(state)
        open_roles = [r for r in ROLES if r not in ev.our_roles.values()]
        unavailable = state.unavailable()
        cands = {}
        for r in open_roles:
            pool = [c for c in self.team.pools.get(r, {}) if c not in unavailable]
            cands[r] = sorted(pool, key=lambda c: (-self._weight(US, c, r), c))[:per_role]
        locked = [(c, ev.our_roles.get(c)) for c in ours]
        target, _ = self.target_for(locked)

        combos = [((a, r1), (b, r2)) for r1, r2 in combinations(open_roles, 2)
                  for a in cands[r1] for b in cands[r2] if a != b]
        roles_of = [{**ev.our_roles, a: r1, b: r2} for (a, r1), (b, r2) in combos]
        syns: list = [None] * len(combos)
        if self.data.synergy_v2 is not None and combos:  # Team vs enemy for every pair in one batch
            syns = self._syn_v2_batch([(ours + [a, b], roles) for ((a, _), (b, _)), roles in zip(combos, roles_of)],
                                      theirs, ev.their_roles)
        scored = []
        for ((a, r1), (b, r2)), roles, syn in zip(combos, roles_of, syns):
            total = self._terms(ours + [a, b], roles, theirs, ev.their_roles, syn).total
            fit = self.coherence(locked, a, r1, target) + self.coherence([*locked, (a, r1)], b, r2, target)
            scored.append((total + fit, total, (a, r1), (b, r2)))
        scored.sort(key=lambda x: (-x[0], x[2][0], x[3][0]))

        valid, seen = [], set()  # (rank score, pair), best first, guardrails checked, each champion pair once
        for score, total, (a, r1), (b, r2) in scored:
            if (key := frozenset((a, b))) in seen:
                continue
            win = sigmoid(total)
            first, second = (Suggestion(c, self.data.name(c), "pick", win, self._pair_reason(c, r), role=r, win_prob=win)
                             for c, r in ((a, r1), (b, r2)))
            if len(validate_suggestions(state, "pick", [first, second])) < 2:
                continue
            seen.add(key)
            syn, low = self.pair_synergy(r1, a, r2, b)
            valid.append((score, PickPair(first, second, win, to_pp(syn), low)))

        out: list[tuple[float, PickPair]] = []
        shown: dict[int, int] = {}  # champion -> pairs it is already in
        while len(out) < k:
            left = [(s, p) for s, p in valid if all(p is not q for _, q in out)]
            if not left:
                break
            pick = max(left, key=lambda sp: sp[0] - self.cfg.pair_repeat_margin  # first of equals wins
                       * (shown.get(sp[1].first.champ_id, 0) + shown.get(sp[1].second.champ_id, 0)))
            out.append(pick)
            for c in (pick[1].first.champ_id, pick[1].second.champ_id):
                shown[c] = shown.get(c, 0) + 1
        return [p for _, p in sorted(out, key=lambda x: -x[0])]

    def pick_record(self, c: int, role: str) -> dict:
        """For display: the champion's meta win rate in that player's bracket, and the player's own record."""
        bracket = self.team.brackets[role]
        stat = self._bracket(bracket).role_stat(c, role)
        entry = self.team.entry(c, role)
        games, wins = (entry.games, entry.wins) if entry else (0, 0)
        return {"meta_wr": stat.win_rate if stat else None, "bracket": bracket, "player_games": games,
                "player_wr": wins / games if games else None}

    def _pair_reason(self, c: int, role: str) -> str:
        entry = self.team.entry(c, role)
        return f"{ROLE_LABELS[role]} {entry.tier if entry else 'off-pool'}"

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
            lane = -self._our_bracket(r).matchup_edge(r, opp, c) if opp is not None else 0.0
            partners = [(self.pair_synergy(r, c, ro, o)[0], o) for o, ro in ev.their_roles.items() if o != c]
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

    def role_weight(self, role: str) -> float:
        return self.cfg.role_weights.get(role, 1.0) if self.cfg.role_weights else 1.0

    def _matchup_term(self, bracket: BracketStats, role: str, c: int, opp: int) -> tuple[float, str]:
        """A reason's (value, text) for the lane opponent's game matchup."""
        return _LO_TO_PROB * bracket.matchup_edge(role, c, opp), f"counters their {self.data.name(opp)}"

    def _lane_note(self, bracket: BracketStats, role: str, c: int, opp: int) -> str | None:
        """The expected gold lead when it's past 300, with the game matchup: figures, not a verdict. The user reads them."""
        gold = bracket.lane_gold_of(role, c, opp)
        if gold is None or abs(gold) < _LANE_MENTION_GOLD:
            return None
        lead = f"{'+' if gold > 0 else '\u2212'}{abs(gold):.0f}"
        return f"{lead} gold at 15 vs {self.data.name(opp)}, game {_pp(25 * bracket.matchup_edge(role, c, opp))}"

    def _pick_reason(self, c: int, role: str, our_roles: dict[int, str], their_roles: dict[int, str],
                     flex_roles: list[str], flex: float, risk: float, counter: int | None,
                     fit: tuple[float, str | None] = (0.0, None)) -> str:
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
            terms.append(self._matchup_term(bracket, role, c, opp))
        for o, r_o in our_roles.items():
            if o != c:
                syn = self.pair_synergy(role, c, r_o, o)[0]
                terms.append((_LO_TO_PROB * syn, f"{_pp(to_pp(syn))} with your locked {self.data.name(o)}"))
        if (bot := self.data.bot) is not None and role in ("adc", "support"):
            other = "support" if role == "adc" else "adc"
            partner = next((o for o, r in our_roles.items() if r == other), None)
            if partner is not None:
                pair = bot.pair(c, partner) if role == "adc" else bot.pair(partner, c)
                label = bot.pair_label(c, role)
                if label and abs(to_pp(pair)) >= _PAIR_MENTION_PP:
                    terms.append((_LO_TO_PROB * pair, f"Your {self.data.name(partner)}: "
                                  f"{label.replace('_', ' ')} partner {_pp(to_pp(pair))}"))
        if flex > 0:
            terms.append((flex, "flex " + "/".join(ROLE_LABELS[r] for r in flex_roles)))
        if fit[0] > 0 and fit[1] is not None:
            terms.append((fit[0], f"fits our {STYLE_LABELS[fit[1]]}"))

        best = max((t for t in terms if t[0] > 0), key=lambda t: t[0], default=None)
        reason = f"{head}, {best[1]}" if best else head
        if counter is not None and risk > (best[0] if best else 0):
            reason += f"; watch for {self.data.name(counter)}"
        if opp is not None and (note := self._lane_note(bracket, role, c, opp)):
            reason += f"; {note}"
        return reason
