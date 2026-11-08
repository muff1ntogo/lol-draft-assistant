"""History's overview band (docs/history-charts.md): what our logged games say across games, under the same filters
as the list. Read-only over drafts, draft_actions, game_execution and own_game_participants; every rate comes with
its game count and a Wilson interval, because a 60% that is 3 of 5 should look as weak as it is."""
from __future__ import annotations

import json
import math
from collections import defaultdict

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.api.recap import _actions, _roles, art, plan_adherence, queue_label
from draftassist.data.provider import ChampionCatalog
from draftassist.db.models import Draft, DraftAction, GameExecution, OwnGameParticipant
from draftassist.engine.engine import DraftEngine
from draftassist.engine.model import ROLE_LABELS, ROLES

ROLLING = 10  # games in the rolling win rate
CALIBRATION_BINS = 10
Z = 1.96  # 95% Wilson interval


def wilson(wins: int, n: int) -> tuple[float, float] | None:
    """95% Wilson score interval for a win rate; None without games."""
    if n == 0:
        return None
    p = wins / n
    denom = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denom
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def rate(wins: int, n: int, **extra) -> dict:
    return {"games": n, "wins": wins, "wr": round(wins / n, 3) if n else None, "ci": wilson(wins, n), **extra}


def _won(d: Draft) -> int:
    return 1 if d.result == "win" else 0


def _filtered(drafts: list[Draft], catalog: ChampionCatalog, queue, side, patch) -> list[Draft]:
    out = []
    for d in drafts:
        d_patch = d.patch or (".".join(catalog.patch.split(".")[:2]) if catalog.patch else None)
        if (queue and queue_label(d) != queue) or (side and d.side != side) or (patch and d_patch != patch):
            continue
        out.append(d)
    return out


def _calibration(games: list[Draft]) -> list[dict]:
    bins: dict[int, list[Draft]] = defaultdict(list)
    for d in games:
        bins[min(int(d.pred_win_prob * CALIBRATION_BINS), CALIBRATION_BINS - 1)].append(d)
    step = 100 // CALIBRATION_BINS
    return [{"bin": f"{b * step}-{b * step + step}%", **rate(sum(map(_won, v)), len(v)),
             "predicted": round(sum(d.pred_win_prob for d in v) / len(v), 3)} for b, v in sorted(bins.items())]


def _trend(games: list[Draft]) -> list[dict]:
    out = []
    for i, d in enumerate(games):
        window = games[max(0, i - ROLLING + 1): i + 1]
        preds = [g.pred_win_prob for g in window if g.pred_win_prob is not None]
        out.append({"game": i + 1, "draft_id": d.draft_id, "played_at": d.played_at.isoformat(), "result": d.result,
                    "pred": None if d.pred_win_prob is None else round(d.pred_win_prob, 3),
                    "rolling_wr": round(sum(map(_won, window)) / len(window), 3),
                    "rolling_pred": round(sum(preds) / len(preds), 3) if preds else None})
    return out


def _split(games: list[Draft], key) -> list[dict]:
    groups: dict[str, list[Draft]] = defaultdict(list)
    for d in games:
        groups[key(d)].append(d)
    out = []
    for name, v in sorted(groups.items()):
        preds = [d.pred_win_prob for d in v if d.pred_win_prob is not None]
        out.append({"name": name, **rate(sum(map(_won, v)), len(v)),
                    "predicted": round(sum(preds) / len(preds), 3) if preds else None})
    return out


def _execution(s: Session, games: list[Draft]) -> list[dict]:
    rows = {e.draft_id: e for e in s.exec(select(GameExecution).where(
        col(GameExecution.draft_id).in_([d.draft_id for d in games]))).all()}
    out = []
    for i, d in enumerate(games):
        e = rows.get(d.draft_id)
        if e is None:
            continue
        out.append({"game": i + 1, "draft_id": d.draft_id, "result": d.result, "verdict": e.verdict,
                    "draft": round(100 * e.draft, 1), "early": None if e.early is None else round(100 * e.early, 1),
                    "conversion": None if e.conversion is None else round(100 * e.conversion, 1)})
    return out


def _players(s: Session, engine: DraftEngine, catalog: ChampionCatalog, games: list[Draft],
             acts: dict[int, list[DraftAction]]) -> list[dict]:
    """Per player (or role, when the name wasn't logged), per champion: games, wins and the mean gold lead at 15."""
    lanes = defaultdict(dict)
    for p in s.exec(select(OwnGameParticipant).where(col(OwnGameParticipant.draft_id).in_([d.draft_id for d in games]))):
        lanes[p.draft_id][(p.champ_id, p.team_id)] = p.lane_gold_15
    cells: dict[tuple[str, str], dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for d in games:
        names = json.loads(d.players_json) if d.players_json else {}
        ours, _ = _roles(engine, d, acts[d.draft_id])
        team = 100 if d.side == "blue" else 200
        for champ, role in ours.items():
            who = names.get(role) or ROLE_LABELS.get(role, role)
            cells[(role, who)][champ].append((_won(d), lanes[d.draft_id].get((champ, team))))
    order = {r: i for i, r in enumerate(ROLES)}
    out = []
    for (role, who), champs in sorted(cells.items(), key=lambda kv: (order.get(kv[0][0], 9), kv[0][1])):
        rows = []
        for champ, results in champs.items():
            golds = [g for _, g in results if g is not None]
            rows.append({"champ": art(catalog, champ, None), **rate(sum(w for w, _ in results), len(results)),
                         "gold15": round(sum(golds) / len(golds)) if golds else None, "gold_games": len(golds)})
        rows.sort(key=lambda r: (-r["games"], -(r["wr"] or 0)))
        out.append({"player": who, "role": role, "role_label": ROLE_LABELS.get(role, role),
                    **rate(sum(r["wins"] for r in rows), sum(r["games"] for r in rows)), "champions": rows})
    return out


def _bans(catalog: ChampionCatalog, games: list[Draft], acts: dict[int, list[DraftAction]]) -> list[dict]:
    """Our most frequent bans and our win rate in the games we banned them. (The draft model's P(win) only moves on
    picks, so a ban's own effect isn't measured here.)"""
    by_champ: dict[int, list[int]] = defaultdict(list)
    for d in games:
        for a in acts[d.draft_id]:
            if a.type == "ban" and a.side == d.side and a.champ_id:
                by_champ[a.champ_id].append(_won(d))
    out = [{"champ": art(catalog, champ, None), **rate(sum(v), len(v))} for champ, v in by_champ.items()]
    out.sort(key=lambda r: (-r["games"], r["champ"]["name"] if r["champ"] else ""))
    return out[:12]


def _followed(games: list[Draft], acts: dict[int, list[DraftAction]]) -> list[dict]:
    """Live games only: did we mostly take our #1 suggestion (half our turns or more)?"""
    groups: dict[str, list[Draft]] = {"Mostly took #1": [], "Mostly didn't": []}
    for d in games:
        if d.imported:
            continue
        turns = [a for a in acts[d.draft_id] if a.side == d.side and a.suggestions_json
                 and json.loads(a.suggestions_json).get("kind") == "suggested"]
        if not turns:
            continue
        top = sum(1 for a in turns if a.our_rank == 1)
        groups["Mostly took #1" if top * 2 >= len(turns) else "Mostly didn't"].append(d)
    return [{"name": k, **rate(sum(map(_won, v)), len(v))} for k, v in groups.items()]


def _plans(games: list[Draft], acts: dict[int, list[DraftAction]]) -> list[dict]:
    labels = {"yes": "Plan followed", "partly": "Partly followed", "no": "Not followed"}
    groups: dict[str, list[Draft]] = defaultdict(list)
    for d in games:
        p = plan_adherence(d, acts[d.draft_id])
        if p is not None:
            groups[p["followed"]].append(d)
    return [{"name": labels[k], **rate(sum(map(_won, groups[k])), len(groups[k]))} for k in labels if groups.get(k)]


def history_stats(db: Engine, engine: DraftEngine, catalog: ChampionCatalog, queue: str | None = None,
                  side: str | None = None, patch: str | None = None) -> dict:
    """Everything the overview band draws. Remakes and games without a result are left out."""
    with Session(db) as s:
        drafts = s.exec(select(Draft).where(col(Draft.result).in_(("win", "loss")))
                        .order_by(col(Draft.played_at), col(Draft.draft_id))).all()
        games = _filtered(drafts, catalog, queue, side, patch)
        acts = _actions(s, [d.draft_id for d in games])
        execution = _execution(s, games)
        players = _players(s, engine, catalog, games, acts)
    predicted = [d for d in games if d.pred_win_prob is not None and not d.imported]
    return {
        "games": len(games), "overall": rate(sum(map(_won, games)), len(games)),
        "calibration": _calibration(predicted), "calibrated_games": len(predicted),
        "trend": _trend(games), "rolling": ROLLING,
        "execution": execution,
        "sides": _split(games, lambda d: d.side), "queues": _split(games, queue_label),
        "players": players, "bans": _bans(catalog, games, acts),
        "followed": _followed(games, acts), "plans": _plans(games, acts),
    }
