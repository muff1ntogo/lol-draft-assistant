"""Post-game draft report (predicted vs actual) and the model check across our logged games.

Spec, Testing: log predicted win % against results; with under 50 games report the trend only
(small samples swing wildly), and the Brier score against a 50% coin flip (0.25).
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.db.models import Draft, DraftAction
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import get_format
from draftassist.engine.model import sigmoid
from draftassist.engine.state import DraftState

COIN_FLIP_BRIER = 0.25
CALIBRATION_MIN_GAMES = 50
TREND_WINDOW = 10


def _pp(x: float) -> float:
    """A log-odds contribution as percentage points of win probability around 50%."""
    return round(100 * (sigmoid(x) - 0.5), 1)


def model_check(db: Engine) -> dict:
    """Brier score of our predictions vs results, its trend, and calibration once there are 50+ games."""
    with Session(db) as s:
        drafts = s.exec(select(Draft).where(col(Draft.result).in_(("win", "loss")), col(Draft.pred_win_prob).is_not(None))
                        .order_by(Draft.played_at)).all()
    games = [(d.pred_win_prob, 1.0 if d.result == "win" else 0.0, d.played_at) for d in drafts]
    n = len(games)
    out: dict = {"games": n, "coin_flip_brier": COIN_FLIP_BRIER, "calibration_min_games": CALIBRATION_MIN_GAMES}
    if not n:
        return {**out, "brier": None, "trend": [], "calibration": None,
                "verdict": "No finished games with a prediction yet."}
    sq = [(p - y) ** 2 for p, y, _ in games]
    brier = sum(sq) / n
    trend = []  # rolling Brier over the last TREND_WINDOW games, game by game
    for i in range(n):
        window = sq[max(0, i - TREND_WINDOW + 1): i + 1]
        trend.append({"game": i + 1, "played_at": games[i][2].isoformat(), "pred": round(games[i][0], 3),
                      "result": "win" if games[i][1] else "loss", "rolling_brier": round(sum(window) / len(window), 4)})
    calibration = None
    if n >= CALIBRATION_MIN_GAMES:
        bins: dict[int, list[tuple[float, float]]] = {}
        for p, y, _ in games:
            bins.setdefault(min(int(p * 10), 9), []).append((p, y))
        calibration = [{"bin": f"{b * 10}-{b * 10 + 10}%", "games": len(v),
                        "predicted": round(sum(p for p, _ in v) / len(v), 3), "actual": round(sum(y for _, y in v) / len(v), 3)}
                       for b, v in sorted(bins.items())]
    better = brier < COIN_FLIP_BRIER
    if n < CALIBRATION_MIN_GAMES:
        verdict = (f"{n} game{'s' if n != 1 else ''}: too few to judge the model (needs {CALIBRATION_MIN_GAMES}); "
                   f"so far it is {'beating' if better else 'not beating'} a coin flip.")
    else:
        verdict = f"Brier {brier:.3f} vs {COIN_FLIP_BRIER} for a coin flip: the model is {'better' if better else 'not better'} than chance."
    return {**out, "brier": round(brier, 4), "skill": round(1 - brier / COIN_FLIP_BRIER, 3),
            "accuracy": round(sum((p > 0.5) == (y == 1.0) for p, y, _ in games) / n, 3),
            "trend": trend, "calibration": calibration, "verdict": verdict}


def draft_report(db: Engine, engine: DraftEngine, draft_id: int) -> dict | None:
    """One game: predicted vs actual, what the model thought decided it, and what our deviations
    from the top suggestion cost or gained (by the model, at the end of the draft)."""
    with Session(db) as s:
        d = s.get(Draft, draft_id)
        if d is None:
            return None
        actions = s.exec(select(DraftAction).where(DraftAction.draft_id == draft_id).order_by(DraftAction.seq)).all()
    fmt = get_format(d.draft_format or "tournament")
    champs = tuple(a.champ_id for a in actions)
    final = DraftState(fmt, d.side, champs)
    ev = engine.evaluate(final)
    t = ev.terms
    predicted = d.pred_win_prob if d.pred_win_prob is not None else ev.win_prob
    actual = 1.0 if d.result == "win" else 0.0 if d.result == "loss" else None
    factors = sorted([
        {"factor": "Our champions' strength", "pp": _pp(t.meta_us)},
        {"factor": "Our comfort on our picks", "pp": _pp(t.comfort)},
        {"factor": "Their champions' strength", "pp": -_pp(t.meta_them)},
        {"factor": "Lane matchups", "pp": _pp(t.lane)},
        {"factor": "Synergy (ours minus theirs)", "pp": _pp(t.syn_us - t.syn_them)},
        {"factor": "Damage balance", "pp": _pp(t.damage)},
    ], key=lambda f: -abs(f["pp"]))

    deviations = []
    taken = set(champs)
    for i, a in enumerate(actions):
        step = fmt.steps[i]
        shown = json.loads(a.suggestions_json) if a.suggestions_json else None
        if step.side != d.side or step.kind != "pick" or not shown or shown["kind"] != "suggested" or a.our_rank == 1:
            continue
        top = shown["items"][0]["champ_id"] if shown["items"] else None
        if top is None or top in taken:
            continue  # the suggestion was taken later by someone: no clean counterfactual
        alt = DraftState(fmt, d.side, champs[:i] + (top,) + champs[i + 1:])
        delta = engine.win_prob(alt) - ev.win_prob
        deviations.append({"step": a.seq, "picked": a.champ_id, "suggested": top, "rank": a.our_rank,
                           "delta_pp": round(100 * delta, 1)})

    played = d.played_at if isinstance(d.played_at, datetime) else datetime.fromisoformat(str(d.played_at))
    if d.imported:  # docs/import-games.md: nothing was predicted at the time
        rated = f"Today's model rates this draft {ev.win_prob:.0%}"
        verdict = f"{rated}: {'won' if actual else 'lost'}." if actual is not None else f"{rated}."
        return {"draft_id": draft_id, "played_at": played.isoformat(), "predicted": None, "result": d.result,
                "brier": None, "verdict": verdict, "factors": factors[:3], "deviations": [],
                "note": "Imported game: nothing was predicted or suggested at the time; the factors are today's "
                        "model, stats and comfort."}
    if actual is None:
        verdict = "No result yet."
    elif (predicted >= 0.5) == (actual == 1.0):
        verdict = f"Called it: predicted {predicted:.0%}, {'won' if actual else 'lost'}."
    else:
        verdict = f"Upset: predicted {predicted:.0%}, {'won' if actual else 'lost'}."
    return {"draft_id": draft_id, "played_at": played.isoformat(), "predicted": round(predicted, 4),
            "result": d.result, "brier": None if actual is None else round((predicted - actual) ** 2, 4),
            "verdict": verdict, "factors": factors[:3], "deviations": deviations,
            "note": "Predicted % is what we showed at lock-in; factors and deviations are re-evaluated with today's "
                    "model, stats and comfort."}
