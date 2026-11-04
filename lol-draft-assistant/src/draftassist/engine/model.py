"""Engine inputs: stats per bracket, our team, and tunable weights. Pure data, no I/O."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from draftassist.engine.comp import EMPTY_PROFILES, ChampionProfiles
from draftassist.engine.mathx import logit, sigmoid
from draftassist.engine.synergy import EMPTY as EMPTY_SYNERGY
from draftassist.engine.synergy import SynergyTable

ROLES: tuple[str, ...] = ("top", "jungle", "mid", "adc", "support")
ROLE_LABELS = {"top": "Top", "jungle": "Jungle", "mid": "Mid", "adc": "ADC", "support": "Support"}

TIERS = ("main", "comfortable", "learning")
# Prior win rate per manual tier (spec, comfort scoring).
TIER_PRIOR_WR = {"main": 0.53, "comfortable": 0.50, "learning": 0.46}


def tier_comfort(tier: str) -> float:
    """v1 comfort from the manual tier: the spec's comfort formula with no recent games."""
    return 0.6 * (TIER_PRIOR_WR[tier] - 0.5) * 10


@dataclass(frozen=True)
class RoleStat:
    win_rate: float  # already shrunk towards 50%
    pick_rate: float  # share of games in this bracket where the champion is picked in this role


@dataclass(frozen=True)
class BracketStats:
    meta: Mapping[int, Mapping[str, RoleStat]]  # champ -> role -> stats
    matchups: Mapping[tuple[str, int, int], float] = field(default_factory=dict)  # (role, a, b) -> a's lane WR
    ban_rates: Mapping[int, float] = field(default_factory=dict)  # champ -> share of games banned
    # docs/matchup-archetypes.md, Stage 3: `matchups` shrink towards a prior that includes both champions' strength
    strength_in_matchups: bool = False
    matchup_games: Mapping[tuple[str, int, int], int] = field(default_factory=dict)  # (role, a, b), a < b
    matchup_k: Mapping[str, float] = field(default_factory=dict)  # role -> k_r
    matchup_strength: Mapping[tuple[int, str], float] = field(default_factory=dict)  # (champ, role) -> log-odds in the prior
    # docs/matchup-archetypes.md, Stage 4: the lane matchup, the mean gold lead at 15 minutes
    lane_gold: Mapping[tuple[str, int, int], float] = field(default_factory=dict)  # (role, a, b), a < b: a's lead
    lane_slope: Mapping[tuple[int, str], float] = field(default_factory=dict)  # (champ, role) -> log-odds per 1,000 gold

    def role_stat(self, champ: int, role: str) -> RoleStat | None:
        return self.meta.get(champ, {}).get(role)

    def matchup_wr_or_none(self, role: str, a: int, b: int) -> float | None:
        if (wr := self.matchups.get((role, a, b))) is not None:
            return wr
        if (wr := self.matchups.get((role, b, a))) is not None:
            return 1 - wr
        return None

    def matchup_wr(self, role: str, a: int, b: int) -> float:
        wr = self.matchup_wr_or_none(role, a, b)
        return 0.5 if wr is None else wr

    def matchup_edge(self, role: str, a: int, b: int) -> float:
        """How much a beats b beyond their strength, in log-odds. P(win), threats and counter risk add it to each
        champion's own strength: counting the whole matchup there would count strength twice."""
        wr = self.matchup_wr_or_none(role, a, b)
        if wr is None:
            return 0.0
        edge = logit(wr)
        if self.strength_in_matchups:
            edge -= self._strength(a, role) - self._strength(b, role)
        return edge

    def _strength(self, champ: int, role: str) -> float:
        if (s := self.matchup_strength.get((champ, role))) is not None:
            return s
        stat = self.role_stat(champ, role)
        return logit(stat.win_rate) if stat else 0.0

    def lane_gold_of(self, role: str, a: int, b: int) -> float | None:
        """a's expected gold lead over b at 15 minutes. None with no lane data."""
        if (gold := self.lane_gold.get((role, a, b))) is not None:
            return gold
        return None if (gold := self.lane_gold.get((role, b, a))) is None else -gold

    def lane_logodds(self, role: str, a: int, b: int) -> float | None:
        """What the lane is worth to a, in log-odds: the lead x the mean of both champions' slopes per 1,000 gold."""
        gold = self.lane_gold_of(role, a, b)
        if gold is None:
            return None
        return gold / 1000 * (self.lane_slope.get((a, role), 1.0) + self.lane_slope.get((b, role), 1.0)) / 2

    def lane_wr(self, role: str, a: int, b: int) -> float | None:
        x = self.lane_logodds(role, a, b)
        return None if x is None else sigmoid(x)

    def blended_wr(self, role: str, a: int, b: int, lane_weight: float) -> float | None:
        """Lane ban Resp and History: the lane and game matchups, weighted. Either one alone if the other is missing."""
        game, lane = self.matchup_wr_or_none(role, a, b), self.lane_wr(role, a, b)
        if lane is None or game is None:
            return game if lane is None else lane
        return lane_weight * lane + (1 - lane_weight) * game

    def counter_term(self, role: str, a: int, b: int, lane_weight: float) -> float:
        """How hard a counters b, in log-odds, for counter risk: the same blend, beyond strength."""
        lane = self.lane_logodds(role, a, b)
        edge = self.matchup_edge(role, a, b)
        return edge if lane is None else lane_weight * lane + (1 - lane_weight) * edge

    def matchup_confidence(self, role: str, a: int, b: int) -> float:
        """games / (games + k_r): 0 = purely an archetype and kit estimate, near 1 = measured."""
        games = self.matchup_games.get((role, min(a, b), max(a, b)), 0)
        return games / (games + self.matchup_k.get(role, 200.0)) if games else 0.0


@dataclass(frozen=True)
class EngineData:
    brackets: Mapping[str, BracketStats]
    names: Mapping[int, str]  # champ_id -> display name
    synergy: SynergyTable = EMPTY_SYNERGY  # role-aware duo synergy, pooled window (engine/synergy.py)
    damage: Mapping[tuple[int, str], tuple[float, float, float]] = field(default_factory=dict)  # (champ, role) -> phys/magic/true share
    profiles: ChampionProfiles = EMPTY_PROFILES  # comp type and fight style per champion (engine/comp.py)
    # Synergy v2 (docs/synergy-v2.md): when set, the synergy term is Stat + Style + Team (engine/synergy_v2.py)
    # instead of `synergy` alone, and the team model's damage rule replaces the damage-balance penalty.
    synergy_v2: object | None = None
    bot: object | None = None  # docs/matchup-archetypes.md, Stage 5: engine/bot_lane.BotLane
    traits: object | None = None  # Stage 6: engine/traits.ChampionTraits, for blind safety and the check strip
    labels: Mapping[tuple[int, str], str] | None = None  # Stage 7: (champ, role) -> main archetype
    explainer: object | None = None  # Stage 7: .explain(role, a, b) -> the matchup's biggest fitted term, or None

    def champions(self) -> frozenset[int]:
        ids = set(self.names)
        for b in self.brackets.values():
            ids.update(b.meta)
        return frozenset(ids)

    def name(self, champ: int) -> str:
        return self.names.get(champ, f"#{champ}")


@dataclass(frozen=True)
class PoolChamp:
    tier: str  # main | comfortable | learning
    comfort: float
    games: int = 0  # the player's recent games with it in this role (last 60 days, learning/comfort.py)
    wins: int = 0


@dataclass(frozen=True)
class OurTeam:
    pools: Mapping[str, Mapping[int, PoolChamp]]  # role -> champ -> entry
    brackets: Mapping[str, str]  # role -> stats bracket of the player in that role

    @classmethod
    def from_tiers(cls, tiers: Mapping[str, Mapping[int, str]], brackets: Mapping[str, str]) -> OurTeam:
        pools = {r: {c: PoolChamp(t, tier_comfort(t)) for c, t in champs.items()} for r, champs in tiers.items()}
        return cls(pools, dict(brackets))

    def entry(self, champ: int, role: str) -> PoolChamp | None:
        return self.pools.get(role, {}).get(champ)

    def roles_for(self, champ: int) -> list[str]:
        return [r for r in ROLES if champ in self.pools.get(r, {})]

    def pool_champs(self) -> set[int]:
        return {c for champs in self.pools.values() for c in champs}


@dataclass(frozen=True)
class LaneBanConfig:
    """Lane ban score options (see engine/lane_ban.py)."""
    adjust_for_bans: bool = True  # w = pick_rate / (1 - ban_rate)
    delta_baseline: Literal["fifty", "champion_avg"] = "fifty"
    tier_adj: Mapping[str, float] = field(
        default_factory=lambda: {"main": 0.0, "comfortable": 0.0, "learning": -2.0})
    pp_to_logodds: float = 0.04  # slope of logit at 50% is 4 per unit probability
    neutral_band: float = 0.05  # |Ban| below this is shown as neutral


@dataclass(frozen=True)
class BanWeights:
    """ban value = relevance * discount * (threat * Threat + matchup * Matchup + comp * Comp)
                   - own * OwnLoss
    The settings slider "Meta threat <-> Our matchups" keeps threat + matchup = 2.5."""
    threat: float = 1.0
    matchup: float = 1.5
    comp: float = 1.0
    own: float = 1.0  # self-ban penalty: what banning one of our own pool champions costs us

    @classmethod
    def from_slider(cls, towards_matchups: float, comp: float = 1.0, own: float = 1.0) -> BanWeights:
        """towards_matchups in [0, 1]: 0 = all meta threat, 1 = all our matchups."""
        t = min(max(towards_matchups, 0.0), 1.0)
        return cls(threat=2.5 * (1 - t), matchup=2.5 * t, comp=comp, own=own)


@dataclass(frozen=True)
class EngineConfig:
    alpha: float = 1.0  # weight on meta strength for our picks
    beta: float = 1.0  # weight on comfort
    off_pool_penalty: float = -0.4  # comfort for a champion outside the player's pool
    off_role_wr: float = 0.40  # win rate assumed for a champion in a role it has no stats for
    enemy_bracket: str = "gold"  # enemy prior
    enemy_strength_weight: float = 10.0  # enemy model: log(pick rate) + w * logit(win rate)
    risk_weight: float = 0.1
    risk_horizon: int = 3  # counter risk is full once the enemy has this many picks left
    flex_bonus: float = 0.02
    lane_ban: LaneBanConfig = field(default_factory=LaneBanConfig)
    ban_weights: BanWeights = field(default_factory=BanWeights)
    ban_declared_intents: bool = False  # standard draft: may we suggest banning a teammate's declared pick?
    damage_balance: bool = True  # penalise a comp that is almost all one damage type
    damage_penalty: float = -0.05  # log-odds when one type is above damage_threshold
    damage_threshold: float = 0.85
    damage_min_champs: int = 3  # don't judge a comp on fewer champions with damage data
    # Comp coherence (docs/comp-style.md Stage C): pick value pulls towards the team style set on the Team
    # page, else the style our comp is converging on. In pick value only, not in P(win): it is a preference,
    # not a measured effect. Damped by how many picks we have locked so it can't take over our first 3 picks.
    comp_coherence: bool = True
    comp_weight: float = 0.15  # log-odds at full strength per unit of fit (a perfect fit ~ +2.5pp of value)
    comp_damping: tuple[float, ...] = (0.0, 0.1, 0.25, 1.0, 1.0)  # by our locked picks: our pick 1 .. 5; real
    # data (16.21): picks 2-3 are near ties (median #1-#2 gap ~0.5pp), so more than this decides them
    comp_target: str | None = None  # the Team page's style: a comp STYLES key, "mixed" (no pull) or None
    # Next 2 picks: cost each time a pair's champion was already shown (log-odds, ~3pp). Real data (16.21,
    # 120 turns): one champion in all 3 pairs 13% -> 1%; the 3rd pair ends ~1.1pp further from the best.
    pair_repeat_margin: float = 0.12
    # docs/matchup-archetypes.md, Stage 4
    lane_weight: float = 0.5  # lane vs game matchup in lane ban Resp, counter risk and History (1 = lane only)
    role_weights: Mapping[str, float] | None = None  # role -> how much its matchups count (None = all 1)
    # docs/matchup-archetypes.md, Stage 6
    blind_weights: Mapping[str, float] = field(default_factory=dict)  # part or "comfort" -> weight (missing = 1)
    comfort_gate: Mapping[str, float] = field(default_factory=dict)  # tier -> share of a counter pick's edge it keeps
    ban_comp_synergy: bool = False  # ban phase 2: Comp also counts synergy with the enemy's locked picks
    min_ban_relevance: float = 0.10  # bans below this share of still-open roles are wasted
    role_share_floor: float = 0.001  # P(champ in role) for roles with no data, when inferring filled roles
