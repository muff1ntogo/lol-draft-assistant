"""Engine inputs: stats per bracket, our team, and tunable weights. Pure data, no I/O."""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

ROLES: tuple[str, ...] = ("top", "jungle", "mid", "adc", "support")
ROLE_LABELS = {"top": "Top", "jungle": "Jungle", "mid": "Mid", "adc": "ADC", "support": "Support"}

TIERS = ("main", "comfortable", "learning")
# Prior win rate per manual tier (spec, comfort scoring).
TIER_PRIOR_WR = {"main": 0.53, "comfortable": 0.50, "learning": 0.46}


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


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
    synergy: Mapping[frozenset[int], float] = field(default_factory=dict)  # {a, b} -> duo WR

    def role_stat(self, champ: int, role: str) -> RoleStat | None:
        return self.meta.get(champ, {}).get(role)

    def matchup_wr(self, role: str, a: int, b: int) -> float:
        if (wr := self.matchups.get((role, a, b))) is not None:
            return wr
        if (wr := self.matchups.get((role, b, a))) is not None:
            return 1 - wr
        return 0.5

    def synergy_wr(self, a: int, b: int) -> float:
        return self.synergy.get(frozenset((a, b)), 0.5)


@dataclass(frozen=True)
class EngineData:
    brackets: Mapping[str, BracketStats]
    names: Mapping[int, str]  # champ_id -> display name

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
class EngineConfig:
    alpha: float = 1.0  # weight on meta strength for our picks
    beta: float = 1.0  # weight on comfort
    off_pool_penalty: float = -0.4  # comfort for a champion outside the player's pool
    off_role_wr: float = 0.40  # win rate assumed for a champion in a role it has no stats for
    enemy_bracket: str = "gold"  # enemy prior
    team_bracket: str = "gold"  # duo synergy for our side (mixed-elo team average)
    enemy_strength_weight: float = 10.0  # enemy model: log(pick rate) + w * logit(win rate)
    protect_weight: float = 0.15
    risk_weight: float = 0.1
    risk_horizon: int = 3  # counter risk is full once the enemy has this many picks left
    flex_bonus: float = 0.02
