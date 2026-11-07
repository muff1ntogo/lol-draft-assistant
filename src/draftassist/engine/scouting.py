"""Enemy scouting in the engine (docs/enemy-scouting.md, Stage B). Pure: data/scout.py builds an EnemyScouting from
the database, and the engine reads it from DraftState.scouting.

The chance that their player in role r is on X blends the bracket's meta with what that player plays:

    weight(X) = (1 - s) * meta(X) + s * share(X) * total(meta),   s = SCOUT_WEIGHT * n / (n + CONFIDENCE_K)

with share(X) their share of games on X in that role (n games). With no games in the role yet, their mastery points
spread over the champions the meta plays in that role, counted as MASTERY_ONLY_GAMES games.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SCOUT_WEIGHT = 0.6  # the spec's 60% player history, 40% meta, at full confidence
CONFIDENCE_K = 5  # games in the role for half of SCOUT_WEIGHT
MASTERY_ONLY_GAMES = 5  # what a mastery-only pool counts as
MASTERY_ROLE_SHARE = 0.10  # mastery spreads over champions the meta plays at least this much in the role
POOL_MIN_GAMES = 2  # "in their pool": this many recent games on it, in any role ...
POOL_TOP_MASTERY = 3  # ... or one of their top mastery champions


@dataclass(frozen=True)
class ScoutedPlayer:
    name: str  # the game name, for reasons ("their top (Name)")
    role: str
    games: tuple[tuple[int, int, int], ...] = ()  # (champ, games, wins) in this role
    any_role: tuple[tuple[int, int], ...] = ()  # (champ, games) in every role
    mastery: tuple[tuple[int, int], ...] = ()  # (champ, points), highest first

    @property
    def n(self) -> int:
        return sum(g for _, g, _ in self.games)

    def record(self, champ: int) -> tuple[int, int]:
        return next(((g, w) for c, g, w in self.games if c == champ), (0, 0))

    def mastery_rank(self, champ: int) -> int | None:
        return next((i + 1 for i, (c, _) in enumerate(self.mastery) if c == champ), None)

    def plays(self, champ: int) -> bool:
        """In their pool: recent games on it, or one of their top mastery champions."""
        games = next((g for c, g in self.any_role if c == champ), 0)
        rank = self.mastery_rank(champ)
        return games >= POOL_MIN_GAMES or (rank is not None and rank <= POOL_TOP_MASTERY)

    def share(self, role_shares: Mapping[int, Mapping[str, float]]) -> tuple[dict[int, float], int]:
        """Their share per champion in this role, and how many games it rests on."""
        if self.n:
            return {c: g / self.n for c, g, _ in self.games}, self.n
        points = {c: p for c, p in self.mastery if role_shares.get(c, {}).get(self.role, 0.0) >= MASTERY_ROLE_SHARE}
        total = sum(points.values())
        if total <= 0:
            return {}, 0
        return {c: p / total for c, p in points.items()}, MASTERY_ONLY_GAMES


@dataclass(frozen=True)
class EnemyScouting:
    players: tuple[ScoutedPlayer, ...]
    weight: float = SCOUT_WEIGHT

    def player(self, role: str) -> ScoutedPlayer | None:
        return next((p for p in self.players if p.role == role), None)

    def strength(self, n: int) -> float:
        return self.weight * n / (n + CONFIDENCE_K) if n > 0 else 0.0

    def owner(self, champ: int, open_roles: Iterable[str] | None = None) -> ScoutedPlayer | None:
        """The enemy player whose pool holds `champ` (most games on it first), among the roles still open."""
        roles = set(open_roles) if open_roles is not None else None
        found = [p for p in self.players if (roles is None or p.role in roles) and p.plays(champ)]
        return max(found, key=lambda p: (p.record(champ)[0], -(p.mastery_rank(champ) or 99)), default=None)

    def blend(self, role: str, meta: Mapping[int, float], role_shares: Mapping[int, Mapping[str, float]],
              allowed: Iterable[int] | None = None) -> dict[int, float]:
        """Meta weights for a role (any scale) -> blended weights on the same total. `allowed`: champions that can
        still be picked (their share is renormalised over these)."""
        player = self.player(role)
        total = sum(meta.values())
        if player is None or total <= 0:
            return dict(meta)
        share, n = player.share(role_shares)
        if allowed is not None:
            ok = set(allowed)
            share = {c: v for c, v in share.items() if c in ok}
            z = sum(share.values())
            share = {c: v / z for c, v in share.items()} if z > 0 else {}
        s = self.strength(n)
        if not share or s <= 0:
            return dict(meta)
        return {c: (1 - s) * meta.get(c, 0.0) + s * share.get(c, 0.0) * total for c in set(meta) | set(share)}


class ScoutedLaneData:
    """A LaneBanData provider whose opponent weights are blended with the scouted players' pools (engine/lane_ban.py):
    so Threat and Matchup see what their player in each lane actually plays."""

    def __init__(self, base, scouting: EnemyScouting, role_shares: Mapping[int, Mapping[str, float]]):
        self._base, self._scouting, self._role_shares = base, scouting, role_shares
        self._weights: dict[str, dict[int, float]] = {}

    def _role(self, role: str) -> dict[int, float]:
        if role not in self._weights:
            meta = {c: pr for c in self._base.opponents(role) if (pr := self._base.pick_rate(c, role)) > 0}
            self._weights[role] = self._scouting.blend(role, meta, self._role_shares)
        return self._weights[role]

    def opponents(self, role: str) -> Iterable[int]:
        return sorted(self._role(role))

    def pick_rate(self, champ: int, role: str) -> float:
        return self._role(role).get(champ, 0.0)

    def __getattr__(self, name):  # ban rates, matchups, role win rates, the bot lane: as the base has them
        return getattr(self._base, name)
