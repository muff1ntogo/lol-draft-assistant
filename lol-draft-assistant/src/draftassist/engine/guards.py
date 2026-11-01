"""Last check before any suggestion list leaves the engine. Re-checks, independently of the scorers:

1. no banned or picked champion (DraftState.unavailable() is the only availability source),
2. bans: relevance >= min_ban_relevance (no ban on a role the enemy already filled),
3. distinct champions; bans with value <= 0 are not suggestions.

With DRAFTASSIST_STRICT=1 (tests, dev) a violation raises. Otherwise the entry is dropped and a
warning with the draft state is logged, so the shot-caller never sees a bad suggestion.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

from draftassist.engine.state import DraftState

if TYPE_CHECKING:
    from draftassist.engine.engine import Suggestion

log = logging.getLogger(__name__)

DEFAULT_MIN_BAN_RELEVANCE = 0.10


class GuardViolation(ValueError):
    pass


def strict_mode() -> bool:
    return os.environ.get("DRAFTASSIST_STRICT") == "1"


def _violation(state: DraftState, kind: str, s: Suggestion, seen: set[int], unavailable: frozenset[int],
               min_ban_relevance: float) -> str | None:
    if s.champ_id in unavailable:
        return "unavailable champion (already banned or picked)"
    if s.champ_id in seen:
        return "duplicate champion"
    if kind == "ban":
        if s.relevance is None or s.relevance < min_ban_relevance:
            return f"ban relevance {s.relevance} below {min_ban_relevance} (role already filled by the enemy)"
        if s.value <= 0:
            return f"ban value {s.value:.4f} is not positive"
    return None


def validate_suggestions(state: DraftState, kind: str, suggestions: Sequence[Suggestion],
                         min_ban_relevance: float = DEFAULT_MIN_BAN_RELEVANCE) -> list[Suggestion]:
    unavailable = state.unavailable()
    strict = strict_mode()
    kept: list[Suggestion] = []
    seen: set[int] = set()
    for s in suggestions:
        problem = _violation(state, kind, s, seen, unavailable, min_ban_relevance)
        if problem is None:
            kept.append(s)
            seen.add(s.champ_id)
            continue
        message = (f"{kind} suggestion {s.name} ({s.champ_id}): {problem}; "
                   f"format={state.format.name} side={state.our_side} actions={state.actions} hovers={state.hovers}")
        if strict:
            raise GuardViolation(message)
        log.warning("dropped %s", message)
    return kept
