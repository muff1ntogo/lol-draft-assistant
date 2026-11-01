"""Immutable draft state: a format, our side, and the champion chosen at each completed step."""
from __future__ import annotations

from dataclasses import dataclass

from draftassist.engine.formats import DraftFormat, Side, Step

SKIPPED = 0  # LCU reports a skipped ban as championId 0


@dataclass(frozen=True)
class DraftState:
    format: DraftFormat
    our_side: Side
    actions: tuple[int, ...] = ()
    # Teammate pick intents: (champion, role). Provisional; never counted as picks.
    hovers: tuple[tuple[int, str], ...] = ()
    # Bans assumed for scoring only: a group ban set being chosen greedily, or teammates' ban hovers.
    provisional: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if len(self.actions) > len(self.format):
            raise ValueError("more actions than steps in the draft format")

    @property
    def their_side(self) -> Side:
        return "red" if self.our_side == "blue" else "blue"

    @property
    def step(self) -> Step | None:
        n = len(self.actions)
        return self.format.steps[n] if n < len(self.format) else None

    @property
    def is_complete(self) -> bool:
        return self.step is None

    @property
    def is_our_turn(self) -> bool:
        return self.step is not None and self.step.side == self.our_side

    def _done(self) -> list[tuple[Step, int]]:
        return list(zip(self.format.steps, self.actions))

    def bans(self, side: Side | None = None) -> list[int]:
        return [c for s, c in self._done() if s.kind == "ban" and c != SKIPPED and side in (None, s.side)]

    def picks(self, side: Side) -> list[int]:
        return [c for s, c in self._done() if s.kind == "pick" and s.side == side]

    @property
    def our_picks(self) -> list[int]:
        return self.picks(self.our_side)

    @property
    def their_picks(self) -> list[int]:
        return self.picks(self.their_side)

    def unavailable(self) -> frozenset[int]:
        """The single source of truth for availability: every completed ban or pick by either
        team, plus provisional bans while a ban set is being chosen. Skipped bans (0) and pick
        hovers don't count."""
        return frozenset(c for c in self.actions if c != SKIPPED) | self.provisional

    def with_provisional(self, champs) -> DraftState:
        return DraftState(self.format, self.our_side, self.actions, self.hovers, self.provisional | frozenset(champs))

    def hovered_by(self, champ: int) -> str | None:
        return next((role for c, role in self.hovers if c == champ), None)

    def picks_left(self, side: Side, after: int | None = None) -> int:
        """Pick steps for `side` strictly after step index `after` (default: the current step)."""
        start = (len(self.actions) if after is None else after) + 1
        return sum(1 for s in self.format.steps[start:] if s.kind == "pick" and s.side == side)

    def apply(self, champ: int) -> DraftState:
        step = self.step
        if step is None:
            raise ValueError("draft is already complete")
        if champ == SKIPPED and step.kind != "ban":
            raise ValueError("only a ban can be skipped")
        if champ != SKIPPED and champ in self.unavailable():
            raise ValueError(f"champion {champ} is already banned or picked")
        return DraftState(self.format, self.our_side, self.actions + (champ,))  # hovers are per-event
