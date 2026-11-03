"""Plan-driven bans and picks: the Prep page's bottom half (docs/synergy-v2.md, Stage 5). Pure, no I/O.

A plan is our champion per role for some roles (the Prep page's locks). Everything is computed for the plan,
our side and the draft format, from a DraftState (empty before the draft, the live one with "use current
draft"). Plan champions are never suggested as bans, and every list goes through the guardrails.

Bans (top 5 each, value > 0, in log-odds):
- Win lane: the lane ban score (engine/lane_ban.py) with each plan champion the only responder in its role;
  roles not in the plan use the player's pool. 0.04 x TeamLaneBan.
- Win teamfights: P(enemy picks X) x dTeam(X), where dTeam is how much X in an average enemy comp lowers our
  plan's Team vs enemy (engine/team.py), cross terms included, averaged over X's likely open roles.
  P(enemy picks X) = X's per-team pick rate in the enemy bracket, given it isn't banned.
- Combined: the engine's ban value with the plan treated as locked (Matchup, Comp), plus d x Win teamfights.
Both lists are scaled by relevance and, in Flex (blind simultaneous bans), by 1 - P(the enemy bans it too).
Flex Combined uses the engine's coordinated group bans (one per teammate).

Picks:
- Pick order: plan champions mapped onto our pick slots, choosing the assignment that minimises expected
  loss: contest (P(banned or taken before the slot)) x what losing it costs (vs its backup), plus counter
  risk (the best open counter's lane edge, scaled by the enemy picks left after the slot, weighted as in
  the engine), minus the flex value of hiding a multi-role champion while the enemy still picks.
- Fills: for roles not in the plan, the best pool champions by Overall gain (engine/synergy_v2.py).
- Risks: the enemy champions and comp identities that most lower Team vs enemy; a backup per plan champion.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from itertools import permutations

import numpy as np

from draftassist.engine.engine import DraftEngine, Evaluation, Suggestion, enemy_team_ban_prob
from draftassist.engine.formats import DraftFormat
from draftassist.engine.guards import validate_suggestions
from draftassist.engine.lane_ban import RoleBanTable, team_lane_ban
from draftassist.engine.model import ROLE_LABELS, ROLES, logit
from draftassist.engine.state import DraftState
from draftassist.engine.synergy_v2 import Suggestion as Fill
from draftassist.engine.synergy_v2 import SynergyEvaluator
from draftassist.engine.team import IDENTITIES, IDENTITY_LABELS, TeamRule

K = 5
MIN_PRESENCE = 0.002  # enemy champions picked less than this aren't Win-teamfights candidates
MIN_ROLE_SHARE = 0.10
NO_BACKUP_LOSS_PP = 5.0  # losing a plan champion with nobody in the pool to replace it
COUNTERED_PP = 0.5  # counter cost (pp) above which a pick is "countered", below: "safe blind"
IDENTITY_POOL = 12  # per role: the enemy's most-picked champions an identity comp is built from


def _pp(x: float) -> str:
    return f"{x:+.1f}".replace("-", "−") + "pp"


def _pct(p: float) -> str:
    return f"{100 * p:.0f}%"


@dataclass(frozen=True)
class PlanBan(Suggestion):
    owner: str | None = None  # Flex group bans: the teammate who bans it
    teamfight: float = 0.0  # Win teamfights value folded in (Combined), log-odds


@dataclass(frozen=True)
class Teamfight:
    champ: int
    presence: float  # P(enemy picks it)
    delta: float  # how much it lowers our Team vs enemy (log-odds), expected over its open roles
    rule: TeamRule | None  # the rule it hurts us most through
    rule_delta: float
    role: str | None

    @property
    def value(self) -> float:
        return self.presence * self.delta


@dataclass
class PlanContext:
    engine: DraftEngine
    ev: SynergyEvaluator
    plan: Mapping[str, int]
    state: DraftState

    @cached_property
    def unavailable(self) -> frozenset[int]:
        return self.state.unavailable()

    @cached_property
    def evaluation(self) -> Evaluation:
        return self.engine.evaluate(self.state)

    @cached_property
    def active(self) -> dict[str, int]:
        """Plan champions still available, or picked by us (in their role)."""
        ours = set(self.state.our_picks)
        return {r: c for r, c in self.plan.items() if c not in self.unavailable or c in ours}

    @cached_property
    def plan_champs(self) -> frozenset[int]:
        return frozenset(self.plan.values())

    @property
    def flex(self) -> bool:
        return self.state.format.simultaneous_bans

    @cached_property
    def relevance(self) -> dict[int, float]:
        return self.engine.ban_relevance(self.state)

    def discount(self, champ: int) -> float:
        if not self.flex:
            return 1.0
        return 1 - enemy_team_ban_prob(self.engine.enemy_stats.ban_rates.get(champ, 0.0))

    def lane_tables(self) -> dict[str, RoleBanTable]:
        return self.engine.lane_ban_tables(self.state, self.active)

    def ours(self) -> list[tuple[int, str]]:
        return [(c, r) for r, c in self.active.items()]

    def theirs(self) -> list[tuple[int, str]]:
        return [(c, r) for c, r in self.evaluation.their_roles.items()]

    @cached_property
    def teamfight(self) -> dict[int, Teamfight]:
        return _teamfight_table(self)


def enemy_presence(engine: DraftEngine, champ: int) -> float:
    """P(the enemy team picks X): its pick rate summed over roles (picks per match, both teams) halved,
    given it isn't banned."""
    enemy = engine.enemy_stats
    rate = sum(s.pick_rate for r in ROLES if (s := enemy.role_stat(champ, r)) is not None)
    available = max(1 - enemy.ban_rates.get(champ, 0.0), 0.05)
    return min(rate / 2 / available, 0.95)


def _finish(c: PlanContext, bans: list[PlanBan], k: int) -> list[PlanBan]:
    bans = [b for b in bans if b.value > 0 and b.champ_id not in c.plan_champs and b.champ_id not in c.unavailable
            and (b.relevance or 0) >= c.engine.cfg.min_ban_relevance]
    bans.sort(key=lambda b: (-b.value, b.champ_id))
    return validate_suggestions(c.state, "ban", bans[:k], c.engine.cfg.min_ban_relevance, forbidden=c.plan_champs)


# ---------- Win lane ----------
def win_lane_bans(c: PlanContext, k: int = K) -> list[PlanBan]:
    eng = c.engine
    tables = c.lane_tables()
    lane = team_lane_ban(tables.values())
    out = []
    for x, total_pp in lane.items():
        if x in c.unavailable or x in c.plan_champs:
            continue
        rel = c.relevance.get(x, 0.0)
        matchup = eng.cfg.lane_ban.pp_to_logodds * total_pp
        value = rel * c.discount(x) * matchup
        if value <= 0:
            continue
        role = max((r for r in tables if tables[r].row(x)), key=lambda r: tables[r].row(x).ban)
        out.append(PlanBan(x, eng.data.name(x), "ban", value, _lane_reason(c, tables, role, x), role=role,
                           relevance=rel, matchup=matchup, discount=c.discount(x)))
    return _finish(c, out, k)


def _lane_reason(c: PlanContext, tables: Mapping[str, RoleBanTable], role: str, x: int) -> str:
    name = c.engine.data.name
    row = tables[role].row(x)
    if role in c.active and row.via is None:
        return f"Protects {name(c.active[role])}: no matchup data into it ({_pp(row.resp)} baseline)"
    if role in c.active:
        common = sorted(tables[role].rows, key=lambda r: -r.pick_rate)[:10]
        worst = min((r.resp for r in common), default=row.resp)
        what = "worst common lane" if row.resp <= worst else "a bad common lane"
        return f"Protects {name(c.active[role])}: {what} ({_pp(row.resp)})"
    if row.via is not None:
        return f"Our {ROLE_LABELS[role]}'s best answer is {_pp(row.resp)} ({name(row.via)})"
    return f"Our {ROLE_LABELS[role]} pool has no answer to it"


# ---------- Win teamfights ----------
def _teamfight_table(c: PlanContext) -> dict[int, Teamfight]:
    eng, team = c.engine, c.ev.team
    ours = c.ours()
    if not ours or not team.rules:
        return {}
    theirs = c.theirs()
    open_roles = [r for r in ROLES if r not in {r for _, r in theirs}]
    base_total, base_parts = team.vs_batch(ours, [theirs])
    cands: list[tuple[int, str, float]] = []
    for x in eng.champion_ids:
        if x in c.unavailable or x in c.plan_champs or enemy_presence(eng, x) < MIN_PRESENCE:
            continue
        shares = {r: s for r, s in eng.role_shares(x).items() if r in open_roles and s >= MIN_ROLE_SHARE}
        z = sum(shares.values())
        cands += [(x, r, s / z) for r, s in shares.items()]
    if not cands:
        return {}
    totals, parts = team.vs_batch(ours, [theirs + [(x, r)] for x, r, _ in cands])
    rules = team.own_rules + team.cross_rules
    delta: dict[int, float] = {}
    rule_delta: dict[int, np.ndarray] = {}
    best_role: dict[int, tuple[float, str]] = {}
    for (x, r, s), total, p in zip(cands, totals, parts):
        d = float(base_total[0] - total)
        delta[x] = delta.get(x, 0.0) + s * d
        rule_delta[x] = rule_delta.get(x, 0.0) + s * (base_parts[0] - p)
        if x not in best_role or s > best_role[x][0]:
            best_role[x] = (s, r)
    out = {}
    for x, d in delta.items():
        rd = rule_delta[x]
        i = int(np.argmax(rd))
        out[x] = Teamfight(x, enemy_presence(eng, x), d, rules[i] if rd[i] > 0 else None, float(rd[i]),
                           best_role[x][1])
    return out


def _tf_reason(t: Teamfight) -> str:
    if t.rule is None:
        return f"Weakens our teamfight ({_pp(-25 * t.delta)})"
    return f"{t.rule.note} ({_pp(-25 * t.rule_delta)})"


def win_teamfight_bans(c: PlanContext, k: int = K) -> list[PlanBan]:
    eng = c.engine
    out = []
    for x, t in c.teamfight.items():
        rel = c.relevance.get(x, 0.0)
        value = rel * c.discount(x) * t.value
        if value <= 0:
            continue
        out.append(PlanBan(x, eng.data.name(x), "ban", value, _tf_reason(t), role=t.role, enemy_prob=t.presence,
                           relevance=rel, discount=c.discount(x), teamfight=t.value))
    return _finish(c, out, k)


# ---------- Combined ----------
def combined_bans(c: PlanContext, d_weight: float = 1.0, k: int = K) -> list[PlanBan]:
    eng = c.engine
    tf = c.teamfight
    extra = {x: d_weight * t.value for x, t in tf.items()}

    def wrap(s: Suggestion, owner: str | None = None) -> PlanBan:
        t = tf.get(s.champ_id)
        part = extra.get(s.champ_id, 0.0)
        w = eng.cfg.ban_weights
        others = max(w.threat * (s.threat or 0), w.matchup * (s.matchup or 0), w.comp * (s.comp or 0))
        reason = _tf_reason(t) if t is not None and part > 0 and part > others else s.reason
        fields = {f: getattr(s, f) for f in Suggestion.__dataclass_fields__}
        return PlanBan(**(fields | {"reason": reason}), owner=owner, teamfight=part)

    if c.flex and c.state.step is not None and c.state.step.kind == "ban":
        group = eng.ban_group(c.state, list(ROLES), c.active, extra, exclude=c.plan_champs)
        return [wrap(s, owner) for owner, s in group if s is not None][:k]
    out = [wrap(s) for s in eng.ban_suggestions(c.state, c.active, extra) if s.champ_id not in c.plan_champs]
    return _finish(c, out[: k * 2], k)


# ---------- pick order ----------
@dataclass(frozen=True)
class Slot:
    n: int  # our pick number, 0-4
    index: int  # step index in the format
    label: str  # B1 .. B5 / R1 .. R5
    group: str  # "B2–B3": our picks with no enemy pick between them
    enemy_picks_before: int
    enemy_picks_after: int
    enemy_bans_before: float  # share of the enemy's bans made before this slot


def our_slots(fmt: DraftFormat, side: str) -> list[Slot]:
    letter = "B" if side == "blue" else "R"
    steps = fmt.steps
    ours = [s for s in steps if s.kind == "pick" and s.side == side]
    enemy_picks = [s.index for s in steps if s.kind == "pick" and s.side != side]
    enemy_bans = [s.index for s in steps if s.kind == "ban" and s.side != side]
    groups: list[list[int]] = []
    for i, s in enumerate(ours):
        # a run of our picks with no enemy pick between them; the last pick phase is one group
        # (tournament R4-R5), as the spec writes the slots
        same_run = groups and ours[i - 1].phase == s.phase and (
            s.phase > 1 or not any(ours[i - 1].index < e < s.index for e in enemy_picks))
        if same_run:
            groups[-1].append(i)
        else:
            groups.append([i])
    label = {i: f"{letter}{i + 1}" for i in range(len(ours))}
    group_of = {i: "–".join(dict.fromkeys([label[g[0]], label[g[-1]]])) for g in groups for i in g}
    out = []
    for i, s in enumerate(ours):
        bans_before = 1.0 if fmt.simultaneous_bans else (
            sum(e < s.index for e in enemy_bans) / len(enemy_bans) if enemy_bans else 0.0)
        out.append(Slot(i, s.index, label[i], group_of[i], sum(e < s.index for e in enemy_picks),
                        sum(e > s.index for e in enemy_picks), bans_before))
    return out


@dataclass(frozen=True)
class PlannedPick:
    champ: int
    role: str
    slot_index: int  # our pick number
    label: str
    group: str
    contest: float  # P(banned or taken before that slot)
    counter: int | None  # the best open counter
    counter_edge_pp: float  # its lane edge vs our champion (pp)
    counter_pp: float  # counter risk cost at that slot (pp, engine weighting)
    loss_pp: float  # what losing it to a ban or the enemy costs (pp, vs the backup)
    priority_pp: float  # early priority = contest - counter + flex (pp)
    line: str


def _counter(c: PlanContext, champ: int, role: str) -> tuple[int | None, float]:
    eng = c.engine
    lane = eng.our_stats(role)
    enemy = eng.enemy_stats
    best, who = 0.0, None
    for e in eng.champion_ids:
        if e == champ or e in c.unavailable or e in c.plan_champs or not enemy.role_stat(e, role):
            continue
        if (x := logit(lane.matchup_wr(role, e, champ))) > best:
            best, who = x, e
    return who, best


@dataclass(frozen=True)
class _PickCosts:
    """One plan champion's costs per slot, in pp of win chance (docs/synergy-v2.md, S5-6)."""
    champ: int
    role: str
    loss: float  # what losing it costs (vs its backup)
    counter: int | None  # the best open counter
    edge: float  # that counter's lane edge, log-odds
    roles: int  # pool roles it can play (flex)
    ban_q: float  # P(the enemy team bans it)
    presence: float  # P(the enemy picks it)
    risk_weight: float
    risk_horizon: int
    flex_bonus: float

    def contest(self, s: Slot) -> float:
        """P(banned or taken before slot s)."""
        return 1 - (1 - self.ban_q * s.enemy_bans_before) * (1 - self.presence * s.enemy_picks_before / 5)

    def counter_pp(self, s: Slot) -> float:
        return 100 * self.risk_weight * self.edge * min(1.0, s.enemy_picks_after / self.risk_horizon)

    def flex_pp(self, s: Slot) -> float:
        return 100 * self.flex_bonus * max(self.roles - 1, 0) * s.enemy_picks_after / 5

    def score(self, s: Slot) -> float:
        return -self.contest(s) * self.loss - self.counter_pp(s) + self.flex_pp(s)


def _pick_costs(c: PlanContext, champ: int, role: str, backup: Backup | None) -> _PickCosts:
    eng, cfg = c.engine, c.engine.cfg
    if backup is None:
        loss = NO_BACKUP_LOSS_PP
    else:
        strength = eng.pick_weight(champ, role) - eng.pick_weight(backup.champ, role)
        loss = max(0.0, 25 * (strength + backup.drop))
    who, edge = _counter(c, champ, role)
    return _PickCosts(champ, role, loss, who, edge, len(eng.team.roles_for(champ)),
                      enemy_team_ban_prob(eng.enemy_stats.ban_rates.get(champ, 0.0)), enemy_presence(eng, champ),
                      cfg.risk_weight, cfg.risk_horizon, cfg.flex_bonus)


def _pick_line(c: PlanContext, k: _PickCosts, s: Slot, first: Slot) -> str:
    name = c.engine.data.name
    early_counter = k.counter_pp(first)
    countered = early_counter >= COUNTERED_PP and k.counter is not None
    if s.group == first.group:
        tail = f"{name(k.counter)} counters it ({_pp(-25 * k.edge)})" if countered else "safe blind"
        return f"{s.label} {name(k.champ)}: contested {_pct(k.contest(s))}, {tail}"
    if countered:
        return (f"Hold {name(k.champ)} to {s.group.replace('–', '/')}: "
                f"countered by {name(k.counter)} ({_pp(-25 * k.edge)})")
    return f"{s.label} {name(k.champ)}: contested {_pct(k.contest(s))} by then"


def pick_order(c: PlanContext) -> list[PlannedPick]:
    picked = set(c.state.our_picks)
    todo = [(r, ch) for r, ch in c.active.items() if ch not in picked]
    slots = [s for s in our_slots(c.state.format, c.state.our_side) if s.index >= len(c.state.actions)]
    if not todo or not slots:
        return []
    backup = backups(c)
    costs = [_pick_costs(c, ch, r, backup.get(r)) for r, ch in todo[: len(slots)]]

    # the assignment of plan champions to open slots with the lowest total cost (<= 5!/0! = 120 to try)
    best, best_perm = -math.inf, None
    for perm in permutations(range(len(slots)), len(costs)):
        total = sum(k.score(slots[i]) for k, i in zip(costs, perm))
        total -= 1e-9 * sum(i * k.contest(slots[-1]) for k, i in zip(costs, perm))  # ties: most contested first
        if total > best + 1e-12:
            best, best_perm = total, perm

    out = []
    for k, i in sorted(zip(costs, best_perm), key=lambda t: t[1]):
        s = slots[i]
        priority = k.contest(slots[-1]) * k.loss - k.counter_pp(slots[0]) + k.flex_pp(slots[0])
        out.append(PlannedPick(k.champ, k.role, s.n, s.label, s.group, k.contest(s), k.counter, 25 * k.edge,
                               k.counter_pp(s), k.loss, priority, _pick_line(c, k, s, slots[0])))
    return out


# ---------- fills, risks, backups ----------
def fills(c: PlanContext, k: int = 3) -> dict[str, list[Fill]]:
    pools = {r: list(c.engine.team.pools.get(r, {})) for r in ROLES if r not in c.active}
    return c.ev.suggest(c.active, pools, c.unavailable | c.plan_champs, c.theirs(), k=k, roles=list(pools))


@dataclass(frozen=True)
class Backup:
    champ: int
    overall: float  # Overall with the backup in place of the plan champion
    drop: float  # Overall(plan) - Overall(with backup), log-odds


def backups(c: PlanContext) -> dict[str, Backup | None]:
    """For each plan champion: the player's pool champion that keeps Overall highest if it is banned or
    taken (never a banned, picked or other plan champion)."""
    base = c.ev.evaluate(c.active, full=False).overall
    out: dict[str, Backup | None] = {}
    for role in c.plan:
        best = None
        for b in sorted(c.engine.team.pools.get(role, {})):
            if b in c.unavailable or b in c.plan_champs:
                continue
            ov = c.ev.evaluate({**c.active, role: b}, full=False).overall
            if best is None or ov > best.overall:
                best = Backup(b, ov, base - ov)
        out[role] = best
    return out


def plan_risks(c: PlanContext, k: int = 3) -> dict:
    """The enemy champions and comp identities that most lower our plan's Team vs enemy."""
    tf = c.teamfight
    champs = sorted((t for t in tf.values() if t.delta > 0), key=lambda t: (-t.delta, t.champ))[:k]
    out_champs = [{"champ": t.champ, "role": t.role, "delta": t.delta,
                   "rule": t.rule.note if t.rule else None} for t in champs]
    identities = []
    team, eng = c.ev.team, c.engine
    ours = c.ours()
    if ours and team.rules:
        theirs = c.theirs()
        filled = {r for _, r in theirs}
        enemy = eng.enemy_stats
        comps = []
        for ident in IDENTITIES:
            comp = list(theirs)
            for role in ROLES:
                if role in filled:
                    continue
                cands = sorted((x for x in eng.champion_ids if x not in c.unavailable and x not in c.plan_champs
                                and (s := enemy.role_stat(x, role)) and s.pick_rate > 0),
                               key=lambda x: -enemy.role_stat(x, role).pick_rate)[:IDENTITY_POOL]
                if cands:
                    best = max(cands, key=lambda x: (team.identity_parts(x, role)[ident], -x))
                    comp.append((best, role))
            comps.append((ident, comp))
        base, _ = team.vs_batch(ours, [theirs])
        totals, _ = team.vs_batch(ours, [comp for _, comp in comps])
        rows = [{"identity": ident, "name": IDENTITY_LABELS[ident], "delta": float(base[0] - t),
                 "champs": [x for x, _ in comp if (x, _) not in theirs]} for (ident, comp), t in zip(comps, totals)]
        identities = sorted((r for r in rows if r["delta"] > 0), key=lambda r: -r["delta"])[:k]
    return {"champions": out_champs, "identities": identities}


# ---------- Stage 6: a pinned plan in the live draft ----------
PLAN_BAN_BONUS = 0.3  # log-odds on the plan's Combined bans at our ban turns
PLAN_PICK_BONUS = 0.3  # log-odds on plan champions due at our current pick slot


def build_pin(engine: DraftEngine, ev: SynergyEvaluator, locks: Mapping[str, int], ban_bonus: float = PLAN_BAN_BONUS,
              pick_bonus: float = PLAN_PICK_BONUS, d_weight: float = 1.0) -> dict:
    """What "Pin for next game" stores (JSON): the plan, its pick order and Combined bans for both sides and
    both formats (the side and format are only known in champ select), the backups and the bonuses."""
    from draftassist.engine.formats import STANDARD_RANKED, TOURNAMENT

    locks = {r: int(c) for r, c in locks.items()}
    orders: dict[str, dict] = {}
    bans: dict[str, dict] = {}
    backup_map: dict[str, int | None] = {}
    for fmt in (TOURNAMENT, STANDARD_RANKED):
        orders[fmt.name], bans[fmt.name] = {}, {}
        for side in ("blue", "red"):
            ctx = PlanContext(engine, ev, locks, DraftState(fmt, side))
            orders[fmt.name][side] = [{"champ": o.champ, "role": o.role, "slot": o.label, "group": o.group,
                                       "n": o.slot_index, "line": o.line} for o in pick_order(ctx)]
            bans[fmt.name][side] = [b.champ_id for b in combined_bans(ctx, d_weight)]
            if not backup_map:
                backup_map = {r: (b.champ if b else None) for r, b in backups(ctx).items()}
    return {"locks": locks, "orders": orders, "bans": bans, "backups": backup_map,
            "ban_bonus": ban_bonus, "pick_bonus": pick_bonus}


@dataclass(frozen=True)
class PinEffects:
    ban_bonus: dict[int, float]
    pick_bonus: dict[int, float]
    banner: dict  # state (intact | at_risk | broken), champs, at_risk, gone (with backups)


def pin_effects(pin: Mapping, state: DraftState, enemy_hovers: frozenset[int] = frozenset(),
                filled_roles: frozenset[str] = frozenset()) -> PinEffects:
    """The pinned plan against the live draft:
    - banner: broken once a plan champion is gone (banned by anyone or picked by the enemy), at risk while an
      enemy action hovers one, else intact;
    - ban bonus: the plan's Combined bans for this format and side (the engine applies it on our ban turns);
    - pick bonus: plan champions due by our current pick slot group (e.g. B2-B3), not yet picked, whose role
      we haven't filled with someone else. Availability is left to the engine's guardrails."""
    locks = {r: int(c) for r, c in pin["locks"].items()}
    fmt, side = state.format.name, state.our_side
    unavailable, ours = state.unavailable(), set(state.our_picks)
    gone = [{"champ": c, "role": r, "backup": pin.get("backups", {}).get(r)}
            for r, c in locks.items() if c in unavailable and c not in ours]
    at_risk = [c for c in locks.values() if c in enemy_hovers and c not in unavailable]
    status = "broken" if gone else "at_risk" if at_risk else "intact"
    banner = {"state": status, "champs": locks, "at_risk": at_risk, "gone": gone,
              "picked": [c for c in locks.values() if c in ours]}

    ban_bonus = {c: float(pin["ban_bonus"]) for c in pin["bans"].get(fmt, {}).get(side, []) if c not in unavailable}
    pick_bonus: dict[int, float] = {}
    step = state.step
    if step is not None and step.kind == "pick" and step.side == side:
        slots = our_slots(state.format, side)
        n = len(state.our_picks)
        if n < len(slots):
            last_due = max(s.n for s in slots if s.group == slots[n].group)
            for o in pin["orders"].get(fmt, {}).get(side, []):
                c = int(o["champ"])
                if o["n"] <= last_due and c not in ours and o["role"] not in filled_roles:
                    pick_bonus[c] = float(pin["pick_bonus"])
    return PinEffects(ban_bonus, pick_bonus, banner)


__all__ = [
    "Backup",
    "PinEffects",
    "PlanBan",
    "PlanContext",
    "PlannedPick",
    "Slot",
    "Teamfight",
    "backups",
    "build_pin",
    "combined_bans",
    "enemy_presence",
    "fills",
    "our_slots",
    "pick_order",
    "pin_effects",
    "plan_risks",
    "win_lane_bans",
    "win_teamfight_bans",
]
