"""Trends over logged games for the Team page (docs/draft-vs-execution.md, Stage 5).

Reads what Stage 3 stored (`game_execution`, `game_execution_roles`); it never writes. Only games with the whole split
count (a timeline and 15 minutes), so Draft, Early and Conversion are always means over the same games. A band is the
mean ± one standard error (sample SD / √n). Single games are noisy: below MIN_TREND_GAMES there are no numbers.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.db.models import Draft, DraftAction, GameExecution, GameExecutionRole
from draftassist.engine.model import ROLE_LABELS, ROLES

MIN_TREND_GAMES = 10  # the doc's threshold, for the team and for each player
MIN_CHAMP_GAMES = 5
WINDOWS: dict[str, int | None] = {"10": 10, "20": 20, "all": None}
PARTS = ("draft", "early", "conversion")
EARLY_PARTS = ("top", "jungle", "mid", "bot", "objectives")  # the chips; they add up to Early
VERDICTS = ("Won on the draft", "Won the early game", "Comeback", "Inconclusive", "Lost from ahead", "Lost in the early game",
            "Draft-limited")
LEVER_NAMES = {"draft": "Draft", "conversion": "Conversion", "top": "Top's early game", "jungle": "Jungle's early game",
               "mid": "Mid's early game", "bot": "Bot lane's early game", "objectives": "Objectives and team (early game)"}
# Per player: lane gold lead, solo deaths before 14 and CS lead, or the role's own chip stats. Jungle's gold is its label's
# source (decision 21), though not one of its shown stats.
PLAYER_STATS = {"top": ("gold", "cs", "deaths_solo"), "jungle": ("gold", "jungle_cs", "kp"), "mid": ("gold", "cs", "deaths_solo"),
                "adc": ("gold", "cs", "deaths_solo"), "support": ("gold", "vision", "deaths_solo")}
LOWER_IS_BETTER = {"deaths_solo", "deaths_ganked"}


@dataclass(frozen=True)
class Band:
    mean: float
    se: float | None  # None with one value
    n: int

    @property
    def excludes_zero(self) -> bool:
        return self.se is not None and (self.mean + self.se < 0 or self.mean - self.se > 0)

    def as_dict(self) -> dict:
        return {"mean": self.mean, "se": self.se, "n": self.n}


def band(values: Sequence[float]) -> Band | None:
    n = len(values)
    if not n:
        return None
    mean = sum(values) / n
    if n < 2:
        return Band(mean, None, 1)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    return Band(mean, sd / math.sqrt(n), n)


@dataclass(frozen=True)
class TrendGame:
    """One game with the whole split. Parts and shares in pp; z per role and stat, as stored (raw: + is more)."""
    draft_id: int
    played_at: datetime
    parts: Mapping[str, float]
    verdict: str | None
    shares: Mapping[str, float]
    z: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    champs: Mapping[str, int] = field(default_factory=dict)


def _latest(games: Sequence[TrendGame], window: int | None) -> list[TrendGame]:
    ordered = sorted(games, key=lambda g: g.played_at, reverse=True)
    return ordered if window is None else ordered[:window]


def biggest_lever(parts: Mapping[str, Band]) -> dict | None:
    """The part with the most negative mean, if its band excludes 0. Otherwise no clear pattern: a noisier part that may be
    worse isn't passed over for a surer, smaller one."""
    negative = {k: b for k, b in parts.items() if b.mean < 0}
    if not negative:
        return None
    key = min(negative, key=lambda k: negative[k].mean)
    b = negative[key]
    if not b.excludes_zero:
        return None
    return {"part": key, "name": LEVER_NAMES.get(key, key), **b.as_dict()}


def team_trend(games: Sequence[TrendGame], window: int | None) -> dict:
    chosen = _latest(games, window)
    out = {"window": window, "games": len(chosen), "too_few": len(chosen) < MIN_TREND_GAMES,
           "parts": None, "early_parts": None, "verdicts": None, "lever": None}
    if out["too_few"]:
        return out
    parts = {p: band([g.parts[p] for g in chosen]) for p in PARTS}
    early = {r: band([g.shares.get(r, 0.0) for g in chosen]) for r in EARLY_PARTS}
    verdicts = dict.fromkeys(VERDICTS, 0)
    for g in chosen:
        if g.verdict:
            verdicts[g.verdict] = verdicts.get(g.verdict, 0) + 1
    out.update(parts={p: b.as_dict() for p, b in parts.items()}, early_parts={r: b.as_dict() for r, b in early.items()},
               verdicts=verdicts,
               lever=biggest_lever({"draft": parts["draft"], **early, "conversion": parts["conversion"]}))
    return out


def _stat_bands(games: Sequence[TrendGame], role: str) -> list[dict]:
    out = []
    for stat in PLAYER_STATS[role]:
        b = band([g.z[role][stat] for g in games if stat in g.z.get(role, {})])
        if b is not None:
            out.append({"stat": stat, "lower_is_better": stat in LOWER_IS_BETTER, **b.as_dict()})
    return out


def role_trends(games: Sequence[TrendGame], window: int | None) -> list[dict]:
    """Per player (one per role on the Pools page): mean z of the role's stats, and per champion with 5+ games."""
    chosen = _latest(games, window)
    out = []
    for role in ROLES:
        mine = [g for g in chosen if g.z.get(role)]
        row = {"role": role, "name": ROLE_LABELS[role], "games": len(mine), "too_few": len(mine) < MIN_TREND_GAMES,
               "stats": [], "champions": []}
        if not row["too_few"]:
            row["stats"] = _stat_bands(mine, role)
        by_champ: dict[int, list[TrendGame]] = {}
        for g in mine:
            if g.champs.get(role):
                by_champ.setdefault(g.champs[role], []).append(g)
        row["champions"] = [{"champ_id": c, "games": len(gs), "stats": _stat_bands(gs, role)}
                            for c, gs in sorted(by_champ.items(), key=lambda kv: -len(kv[1])) if len(gs) >= MIN_CHAMP_GAMES]
        out.append(row)
    return out


# ---------- from the database ----------
def load_trend_games(db: Engine) -> list[TrendGame]:
    """Logged games with the whole split, newest first."""
    with Session(db) as s:
        rows = s.exec(select(GameExecution, Draft).where(GameExecution.draft_id == Draft.draft_id, col(GameExecution.p15).is_not(None))
                      .order_by(col(Draft.played_at).desc())).all()
        ids = [d.draft_id for _, d in rows]
        roles: dict[int, list[GameExecutionRole]] = {}
        for r in s.exec(select(GameExecutionRole).where(col(GameExecutionRole.draft_id).in_(ids))).all():
            roles.setdefault(r.draft_id, []).append(r)
        picks: dict[int, dict[str, int]] = {}
        sides = {d.draft_id: d.side for _, d in rows}
        for a in s.exec(select(DraftAction).where(col(DraftAction.draft_id).in_(ids), DraftAction.type == "pick")).all():
            if a.side == sides[a.draft_id] and a.role:
                picks.setdefault(a.draft_id, {})[a.role] = a.champ_id
    games = []
    for e, d in rows:
        shares, z = {}, {}
        for r in roles.get(d.draft_id, []):
            shares[r.role] = 100 * r.share
            if r.role in PLAYER_STATS:
                lines = {line["stat"]: line["z"] for line in json.loads(r.stats_json)}
                headline = json.loads(r.headline_json)
                lines.setdefault(headline["stat"], headline["z"])  # jungle's gold
                z[r.role] = lines
        games.append(TrendGame(d.draft_id, d.played_at, {"draft": 100 * e.draft, "early": 100 * e.early, "conversion": 100 * e.conversion},
                               e.verdict, shares, z, picks.get(d.draft_id, {})))
    return games


def trends_payload(db: Engine, window: str, card: Callable[[int], dict | None], names: Mapping[str, str]) -> dict:
    games = load_trend_games(db)
    size = WINDOWS[window]
    roles = role_trends(games, size)
    for r in roles:
        r["player"] = names.get(r["role"]) or None
        for c in r["champions"]:
            c["champ"] = card(c.pop("champ_id"))
    return {"window": window, "windows": list(WINDOWS), "min_games": MIN_TREND_GAMES, "logged": len(games),
            "team": team_trend(games, size), "roles": roles}
