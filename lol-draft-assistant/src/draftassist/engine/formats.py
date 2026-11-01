"""Draft formats as data, so Ranked 5s, Flex and Clash all run through the same engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["blue", "red"]
ActionKind = Literal["ban", "pick"]

_SIDE: dict[str, Side] = {"B": "blue", "R": "red"}


@dataclass(frozen=True)
class Step:
    index: int  # 0-based position in the draft
    side: Side
    kind: ActionKind
    phase: int  # 1 or 2 (ban/pick rounds); standard ranked has only phase 1
    label: str  # "Ban 1", "Pick 2", ...


@dataclass(frozen=True)
class DraftFormat:
    name: str
    steps: tuple[Step, ...]
    simultaneous_bans: bool = False  # all ten ban at once, blind (standard ranked: Solo/Duo, Flex)

    def __len__(self) -> int:
        return len(self.steps)


def _build(name: str, rounds: list[tuple[str, ActionKind, int, str]], simultaneous_bans: bool = False) -> DraftFormat:
    steps: list[Step] = []
    for order, kind, phase, label in rounds:
        for s in order.split():
            steps.append(Step(len(steps), _SIDE[s], kind, phase, label))
    return DraftFormat(name, tuple(steps), simultaneous_bans)


# Ranked 5s and Clash. Order from the spec appendix.
TOURNAMENT = _build("tournament", [
    ("B R B R B R", "ban", 1, "Ban 1"),
    ("B R R B B R", "pick", 1, "Pick 1"),
    ("R B R B", "ban", 2, "Ban 2"),
    ("R B B R", "pick", 2, "Pick 2"),
])

# Solo/Duo and Flex. In the client the 10 bans are simultaneous (neither side sees the
# other's bans until the phase ends); they are modelled here as alternating steps.
STANDARD_RANKED = _build("ranked", [
    ("B R B R B R B R B R", "ban", 1, "Ban"),
    ("B R R B B R R B B R", "pick", 1, "Pick"),
], simultaneous_bans=True)

FORMATS: dict[str, DraftFormat] = {f.name: f for f in (TOURNAMENT, STANDARD_RANKED)}


def get_format(name: str) -> DraftFormat:
    try:
        return FORMATS[name]
    except KeyError:
        raise ValueError(f"unknown draft format {name!r}; expected one of {sorted(FORMATS)}") from None
