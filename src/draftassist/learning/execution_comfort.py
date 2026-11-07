"""Execution-aware comfort (docs/draft-vs-execution.md, Stage 6), behind a Settings flag that's off by default.

For each pool champion, the player's lane gold against expectation (z) in our logged games with it, shrunk towards 0 by
games. A hint shows on the Pools page next to the tier when the lane and the results disagree:
- "Wins, but behind expectation in lane": the comfort win rate is 50%+ and the shrunk z is -HINT_Z or lower.
- "Loses, but ahead of expectation in lane": the comfort win rate is under 50% and the shrunk z is +HINT_Z or higher.

It never changes a tier or comfort: it's read alongside them. Jungle's figure is its gold lead too, the source of its chip's
label (decision 21). Reads Stage 3's tables only.
"""
from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.db.models import Draft, DraftAction, GameExecution, GameExecutionRole
from draftassist.engine.model import ROLES

LANE_Z_K = 5  # shrink the mean z towards 0 by this many games
HINT_Z = 0.5  # the shrunk mean z needed for a hint


def shrunk_z(zs: Sequence[float]) -> float:
    return sum(zs) / (len(zs) + LANE_Z_K) if zs else 0.0


def execution_hint(wr: float, zs: Sequence[float]) -> dict | None:
    """wr: the champion's comfort win rate (wr_adj); zs: lane gold z in each logged game with it."""
    z = shrunk_z(zs)
    detail = f"(lane gold z {z:+.1f}, shrunk, over {len(zs)} logged game{'s' if len(zs) != 1 else ''})".replace("-", "−")
    if wr >= 0.5 and z <= -HINT_Z:
        return {"kind": "behind_in_lane", "text": f"Wins, but behind expectation in lane {detail}", "z": z, "games": len(zs)}
    if wr < 0.5 and z >= HINT_Z:
        return {"kind": "ahead_in_lane", "text": f"Loses, but ahead of expectation in lane {detail}", "z": z, "games": len(zs)}
    return None


def lane_z_by_entry(db: Engine) -> dict[tuple[str, int], list[float]]:
    """(role, champion) -> the lane gold z of each logged game where our player in that role had that champion."""
    with Session(db) as s:
        drafts = {d.draft_id: d.side for d in s.exec(select(Draft).where(
            col(Draft.draft_id).in_(select(GameExecution.draft_id).where(col(GameExecution.p15).is_not(None))))).all()}
        if not drafts:
            return {}
        picks: dict[tuple[int, str], int] = {}
        for a in s.exec(select(DraftAction).where(col(DraftAction.draft_id).in_(list(drafts)), DraftAction.type == "pick")).all():
            if a.side == drafts[a.draft_id] and a.role in ROLES:
                picks[(a.draft_id, a.role)] = a.champ_id
        out: dict[tuple[str, int], list[float]] = {}
        for r in s.exec(select(GameExecutionRole).where(col(GameExecutionRole.draft_id).in_(list(drafts)),
                                                        col(GameExecutionRole.role).in_(ROLES))).all():
            champ = picks.get((r.draft_id, r.role))
            head = json.loads(r.headline_json)
            if champ is not None and head.get("stat") == "gold":
                out.setdefault((r.role, champ), []).append(float(head["z"]))
    return out
