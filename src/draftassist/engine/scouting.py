"""Enemy scouting in the engine (docs/enemy-scouting.md, Stage B). Pure: data/scout.py builds an EnemyScouting from
the database, and the engine reads it from DraftState.scouting.

The chance that their player in role r is on X blends the bracket's meta with what that player plays:

    weight(X) = (1 - s) * meta(X) + s * share(X) * total(meta),   s = SCOUT_WEIGHT * n / (n + CONFIDENCE_K)

with share(X) their share of evidence on X in that role, resting on n (pseudo-)games (Stage E, decision 13):
- their games on X in that role;
- their games on X in other roles, at OFF_ROLE_WEIGHT, if the meta plays X in this role (flex picks, role swaps);
- their mastery, always, as a prior worth MASTERY_ONLY_GAMES games spread over the champions the meta plays in that
  role, by sqrt(points) and how recently they played each (half-life MASTERY_HALF_LIFE_DAYS).
So a player with one game in the role leans on their mastery rather than on that one game, and a main they stopped
playing months ago fades. s is also scaled by how sure we are of their role (`certainty`: 1 when the client gives it).
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SCOUT_WEIGHT = 0.6  # the spec's 60% player history, 40% meta, at full confidence
CONFIDENCE_K = 5  # games in the role for half of SCOUT_WEIGHT
MASTERY_ONLY_GAMES = 5  # what a mastery-only pool counts as
MASTERY_ROLE_SHARE = 0.10  # mastery spreads over champions the meta plays at least this much in the role
POOL_MIN_GAMES = 2  # "in their pool": this many recent games on it, in any role ...
POOL_TOP_MASTERY = 3  # ... or one of their top mastery champions, played in the last STALE_MASTERY_DAYS
OFF_ROLE_WEIGHT = 0.25  # a game on X in another role, as evidence for X in this one
MASTERY_HALF_LIFE_DAYS = 30.0  # a mastery champion last played this long ago counts half
STALE_MASTERY_DAYS = 90.0  # not played for this long: no longer "in their pool" by mastery


def recency(age_days: float | None) -> float:
    """How much a mastery champion last played `age_days` ago still counts (1 when the API didn't say)."""
    return 1.0 if age_days is None else 0.5 ** (max(age_days, 0.0) / MASTERY_HALF_LIFE_DAYS)


@dataclass(frozen=True)
class ScoutedPlayer:
    name: str  # the game name, for reasons ("their top (Name)")
    role: str
    games: tuple[tuple[int, int, int], ...] = ()  # (champ, games, wins) in this role
    any_role: tuple[tuple[int, int], ...] = ()  # (champ, games) in every role
    mastery: tuple[tuple[int, int], ...] = ()  # (champ, points), highest first
    mastery_age: tuple[tuple[int, float], ...] = ()  # (champ, days since they last played it), when the API said
    certainty: float = 1.0  # how sure we are they play `role`: 1 from the client's positions, less when inferred
    # (champ, games x patch weight) in this role (docs/scouting-research.md): when given, the share reads these, so
    # this patch's games count most; `games` stays the plain record for reasons and labels
    weighted: tuple[tuple[int, float], ...] = ()

    @property
    def n(self) -> int:
        return sum(g for _, g, _ in self.games)

    def age(self, champ: int) -> float | None:
        return next((a for c, a in self.mastery_age if c == champ), None)

    def stale(self, champ: int) -> bool:
        """A mastery champion they haven't played for STALE_MASTERY_DAYS."""
        age = self.age(champ)
        return age is not None and age > STALE_MASTERY_DAYS

    def record(self, champ: int) -> tuple[int, int]:
        return next(((g, w) for c, g, w in self.games if c == champ), (0, 0))

    def mastery_rank(self, champ: int) -> int | None:
        return next((i + 1 for i, (c, _) in enumerate(self.mastery) if c == champ), None)

    def plays(self, champ: int) -> bool:
        """In their pool: recent games on it, or one of their top mastery champions."""
        games = next((g for c, g in self.any_role if c == champ), 0)
        rank = self.mastery_rank(champ)
        return games >= POOL_MIN_GAMES or (rank is not None and rank <= POOL_TOP_MASTERY and not self.stale(champ))

    def mastery_share(self, role_shares: Mapping[int, Mapping[str, float]], role: str | None = None) -> dict[int, float]:
        """Their mastery spread over the champions the meta plays in the role: sqrt(points) x recency, summing to 1."""
        role = role or self.role
        weights = {c: math.sqrt(max(p, 0)) * recency(self.age(c)) for c, p in self.mastery
                   if role_shares.get(c, {}).get(role, 0.0) >= MASTERY_ROLE_SHARE}
        total = sum(weights.values())
        return {c: w / total for c, w in weights.items() if w > 0} if total > 0 else {}

    def share(self, role_shares: Mapping[int, Mapping[str, float]]) -> tuple[dict[int, float], float]:
        """Their share per champion in this role, and how many (pseudo-)games it rests on."""
        counts: dict[int, float] = ({c: float(w) for c, w in self.weighted} if self.weighted
                                    else {c: float(g) for c, g, _ in self.games})
        for c, g in self.any_role:  # the same champion in another role: a flex pick or a role swap
            off = g - self.record(c)[0]
            if off > 0 and role_shares.get(c, {}).get(self.role, 0.0) >= MASTERY_ROLE_SHARE:
                counts[c] = counts.get(c, 0.0) + OFF_ROLE_WEIGHT * off
        for c, v in self.mastery_share(role_shares).items():
            counts[c] = counts.get(c, 0.0) + MASTERY_ONLY_GAMES * v
        n = sum(counts.values())
        if n <= 0:
            return {}, 0
        return {c: v / n for c, v in counts.items()}, n


@dataclass(frozen=True)
class EnemyScouting:
    players: tuple[ScoutedPlayer, ...]
    weight: float = SCOUT_WEIGHT

    def player(self, role: str) -> ScoutedPlayer | None:
        return next((p for p in self.players if p.role == role), None)

    def strength(self, n: float) -> float:
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
        s = self.strength(n) * player.certainty
        if not share or s <= 0:
            return dict(meta)
        return {c: (1 - s) * meta.get(c, 0.0) + s * share.get(c, 0.0) * total for c in set(meta) | set(share)}


class ScoutedLaneData:
    """A LaneBanData provider whose opponent weights are blended with the scouted players' pools (engine/lane_ban.py):
    so Threat and Matchup see what their player in each lane actually plays."""

    def __init__(self, base, scouting: EnemyScouting, role_shares: Mapping[int, Mapping[str, float]],
                 allowed: Iterable[int] | None = None):
        """`allowed`: champions still available, so a banned main's share goes to the rest of their pool."""
        self._base, self._scouting, self._role_shares = base, scouting, role_shares
        self._allowed = None if allowed is None else frozenset(allowed)
        self._weights: dict[str, dict[int, float]] = {}

    def _role(self, role: str) -> dict[int, float]:
        if role not in self._weights:
            meta = {c: pr for c in self._base.opponents(role) if (pr := self._base.pick_rate(c, role)) > 0}
            self._weights[role] = self._scouting.blend(role, meta, self._role_shares, self._allowed)
        return self._weights[role]

    def opponents(self, role: str) -> Iterable[int]:
        return sorted(self._role(role))

    def pick_rate(self, champ: int, role: str) -> float:
        return self._role(role).get(champ, 0.0)

    def __getattr__(self, name):  # ban rates, matchups, role win rates, the bot lane: as the base has them
        return getattr(self._base, name)
