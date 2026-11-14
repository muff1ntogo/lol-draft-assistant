"""How well does the draft model predict real games? (spec, Testing and evaluation: Brier and a calibration table on held-out
crawled matches; Phase 6: does a fitted model beat the v1 scorer?)

The stats come from one database (the app's, or a pull of the shared data), the games from another: crawled matches that
started after the stats were built, so no game is in the numbers it is judged with. Each game is split into the draft
model's parts (meta, lane, synergy, team; `DraftEngine.draft_terms`), and every way of combining them is scored out of
fold: a weight fitted on four fifths of the games is judged on the fifth it never saw.

    uv run python -m draftassist.data.draft_eval --stats-db <stats.sqlite> --games-db <crawl.sqlite> --after 2026-11-08
"""
from __future__ import annotations

import argparse
import math
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import Engine, create_engine, text

from draftassist.engine.mathx import logit
from draftassist.engine.model import ROLES

FOLDS = 5
TERMS = ("meta_on", "off_role", "lane", "syn", "team")  # meta_on + off_role x logit(off_role_wr) = the engine's meta
RIDGE = 1.0  # a weak prior on fitted weights: plenty for a handful of features on thousands of games


@dataclass
class Game:
    match_id: str
    bracket: str
    blue: dict[str, int]
    red: dict[str, int]
    blue_won: bool
    fold: int = 0
    terms: dict[str, dict[str, float]] = field(default_factory=dict)  # variant -> part -> blue's log-odds


def fold_of(match_id: str) -> int:
    return zlib.crc32(match_id.encode()) % FOLDS


def load_games(db: Engine, after_ms: int = 0, brackets: Sequence[str] | None = None,
               patch: str | None = None, after_brackets: Sequence[str] | None = None) -> list[Game]:
    """Crawled matches with five known roles a side, started after `after_ms` (only in `after_brackets` when given: a
    tier whose published stats hold hardly any games of the patch can't have taken in the games being judged)."""
    sql = ("SELECT m.match_id, m.bracket, p.team_id, p.role, p.champ_id, p.win FROM crawled_matches m "
           "JOIN match_participants p ON p.match_id = m.match_id WHERE m.game_start_ms > :after")
    args: dict = {"after": after_ms}
    if after_brackets is not None:
        names = {f"ab{i}": b for i, b in enumerate(after_brackets)}
        sql = sql.replace("m.game_start_ms > :after", f"(m.game_start_ms > :after OR m.bracket NOT IN "
                                                      f"({', '.join(':' + k for k in names) or 'NULL'}))")
        args |= names
    if patch:
        sql += " AND m.patch = :patch"
        args["patch"] = patch
    by: dict[str, Game] = {}
    with db.connect() as conn:
        for match_id, bracket, team, role, champ, win in conn.execute(text(sql), args):
            g = by.setdefault(match_id, Game(match_id, bracket, {}, {}, False, fold_of(match_id)))
            (g.blue if team == 100 else g.red)[role] = champ
            if team == 100:
                g.blue_won = bool(win)
    games = [g for g in by.values() if set(g.blue) == set(ROLES) == set(g.red)
             and (brackets is None or g.bracket in brackets)]
    return sorted(games, key=lambda g: g.match_id)


# ---------- scores ----------
def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc(p: np.ndarray, y: np.ndarray) -> float:
    """The chance a random win is rated above a random loss (ties count half)."""
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p))
    sorted_p = p[order]
    i = 0
    while i < len(p):  # average ranks over ties
        j = i
        while j + 1 < len(p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    pos = y == 1
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if not n_pos or not n_neg:
        return 0.5
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def fit_logistic(x: np.ndarray, y: np.ndarray, ridge: float = RIDGE, intercept: bool = True) -> np.ndarray:
    """Newton's method with a ridge on the weights (not the intercept). Returns [intercept, w...] or [w...]."""
    xx = np.column_stack([np.ones(len(x)), x]) if intercept else x
    w = np.zeros(xx.shape[1])
    pen = np.full(xx.shape[1], ridge)
    if intercept:
        pen[0] = 0.0
    for _ in range(50):
        p = sigmoid(xx @ w)
        grad = xx.T @ (p - y) + pen * w
        hess = (xx * (p * (1 - p))[:, None]).T @ xx + np.diag(pen)
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    return w


def predict_logistic(w: np.ndarray, x: np.ndarray, intercept: bool = True) -> np.ndarray:
    return sigmoid((np.column_stack([np.ones(len(x)), x]) if intercept else x) @ w)


def out_of_fold(x: np.ndarray, y: np.ndarray, folds: np.ndarray, intercept: bool = True,
                ridge: float = RIDGE) -> tuple[np.ndarray, np.ndarray]:
    """Each game's P(blue wins) from weights fitted on the other folds; and the weights fitted on every game."""
    p = np.empty(len(y))
    for f in np.unique(folds):
        test = folds == f
        p[test] = predict_logistic(fit_logistic(x[~test], y[~test], ridge, intercept), x[test], intercept)
    return p, fit_logistic(x, y, ridge, intercept)


def scores(p: np.ndarray, y: np.ndarray) -> dict[str, float]:
    q = np.quantile(p, [0.2, 0.8]) if len(p) else (0.5, 0.5)
    low, high = p <= q[0], p >= q[1]
    return {"brier": brier(p, y), "log_loss": log_loss(p, y), "auc": auc(p, y),
            "accuracy": float(np.mean((p > 0.5) == (y == 1))),
            "bottom_fifth_wr": float(y[low].mean()) if low.any() else math.nan,
            "top_fifth_wr": float(y[high].mean()) if high.any() else math.nan,
            "mean_abs_logit": float(np.mean(np.abs(np.log(p / (1 - p)))))}


def calibration(p: np.ndarray, y: np.ndarray, edges: Sequence[float] = (0, .4, .45, .48, .5, .52, .55, .6, 1)) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        at = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        n = int(at.sum())
        out.append({"bin": f"{lo:.0%}-{hi:.0%}", "n": n, "predicted": float(p[at].mean()) if n else None,
                    "won": float(y[at].mean()) if n else None})
    return out


# ---------- the engine's view of each game ----------
@dataclass(frozen=True)
class Variant:
    name: str
    stats_patch: str | None = None  # None: what the app reads
    role_prior_games: float | None = None  # None: provider.ROLE_PRIOR_GAMES
    pair_k: float | None = None  # None: engine/synergy.K
    prev_weight: float | None = None  # None: provider.PREV_PATCH_WEIGHT


def engine_terms(stats_db: Engine, variant: Variant) -> Callable[[Game], dict[str, float]]:
    """The engine built as Live builds it (synergy v2 attached), one per bracket, both teams in the game's bracket."""
    from draftassist.api.main import with_synergy_v2
    from draftassist.data.provider import PREV_PATCH_WEIGHT, ROLE_PRIOR_GAMES, load_engine_inputs
    from draftassist.data.settings_store import engine_config
    from draftassist.engine.engine import DraftEngine
    from draftassist.engine.model import OurTeam

    inputs = load_engine_inputs(stats_db, stats_patch=variant.stats_patch,
                                role_prior_games=variant.role_prior_games or ROLE_PRIOR_GAMES, pair_k=variant.pair_k,
                                prev_weight=PREV_PATCH_WEIGHT if variant.prev_weight is None else variant.prev_weight)
    data = with_synergy_v2(stats_db, inputs)
    cfg = engine_config(stats_db)
    engines: dict[str, DraftEngine] = {}

    def terms(g: Game) -> dict[str, float]:
        engine = engines.get(g.bracket)
        if engine is None:
            bracket = g.bracket if g.bracket in data.brackets else cfg.enemy_bracket
            engine = engines[g.bracket] = DraftEngine(data, OurTeam({}, dict.fromkeys(ROLES, bracket)),
                                                      replace(cfg, enemy_bracket=bracket))
        t = engine.draft_terms(g.blue, g.red)
        t["current"] = engine.draft_logodds(g.blue, g.red)
        stats = engine.enemy_stats
        t["off_role"] = float(sum(stats.role_stat(c, r) is None for r, c in g.blue.items())
                              - sum(stats.role_stat(c, r) is None for r, c in g.red.items()))
        t["meta_on"] = t["meta"] - t["off_role"] * logit(engine.cfg.off_role_wr)
        return t

    terms.alpha = cfg.alpha  # type: ignore[attr-defined]
    return terms


# ---------- the report ----------
def evaluate(games: list[Game], variants: Sequence[str]) -> dict:
    """Every model, scored out of fold. `games` must carry `terms` for each variant."""
    y = np.array([g.blue_won for g in games], dtype=float)
    folds = np.array([g.fold for g in games])
    ones = np.zeros((len(games), 0))
    rows: dict[str, dict] = {}
    p_side, w_side = out_of_fold(ones, y, folds)
    rows["coin flip"] = {**scores(np.full(len(y), 0.5), y)}
    rows["side only"] = {**scores(p_side, y), "weights": {"blue": float(w_side[0])}}
    calib = {}
    for v in variants:
        t = {k: np.array([g.terms[v][k] for g in games]) for k in (*TERMS, "meta", "current")}
        raw = sigmoid(t["current"])
        rows[f"{v}: as the app scores it"] = scores(raw, y)
        p_scale, w_scale = out_of_fold(t["current"][:, None], y, folds, intercept=False)
        rows[f"{v}: one scale"] = {**scores(p_scale, y), "weights": {"scale": float(w_scale[0])}}
        x = np.column_stack([t[k] for k in TERMS])
        p_terms, w_terms = out_of_fold(x, y, folds)
        rows[f"{v}: side + weight per part"] = {
            **scores(p_terms, y), "weights": {"blue": float(w_terms[0]), **dict(zip(TERMS, map(float, w_terms[1:])))}}
        for k in TERMS:
            p_k, w_k = out_of_fold(t[k][:, None], y, folds)
            rows[f"{v}: side + {k} alone"] = {**scores(p_k, y), "weights": {"blue": float(w_k[0]), k: float(w_k[1])}}
        calib[v] = {"as the app scores it": calibration(raw, y), "side + weight per part": calibration(p_terms, y)}
        rows[f"{v}: part sizes"] = {"sd": {k: float(np.std(t[k])) for k in (*TERMS, "current")}}
        p_meta, w_meta = out_of_fold(t["meta"][:, None], y, folds)
        rows[f"{v}: side + meta as the engine has it"] = {**scores(p_meta, y),
                                                          "weights": {"blue": float(w_meta[0]), "meta": float(w_meta[1])}}
    return {"games": len(games), "blue_wr": float(y.mean()), "rows": rows, "calibration": calib}


def format_report(r: dict) -> str:
    lines = [f"{r['games']:,} games, blue won {r['blue_wr']:.1%}. Out of fold ({FOLDS} folds); lower Brier and log loss "
             "are better, AUC 0.5 = no signal.",
             f"{'model':<44} {'Brier':>7} {'logloss':>8} {'AUC':>6} {'acc':>6} {'low 5th':>8} {'top 5th':>8}  weights"]
    for name, s in r["rows"].items():
        if "brier" not in s:
            lines.append(f"{name:<44} " + ", ".join(f"{k} sd {v:.3f}" for k, v in s["sd"].items()))
            continue
        w = ", ".join(f"{k} {v:+.3f}" for k, v in s.get("weights", {}).items())
        lines.append(f"{name:<44} {s['brier']:.4f} {s['log_loss']:8.4f} {s['auc']:6.3f} {s['accuracy']:6.1%} "
                     f"{s['bottom_fifth_wr']:8.1%} {s['top_fifth_wr']:8.1%}  {w}")
    for v, tables in r["calibration"].items():
        for name, table in tables.items():
            lines.append(f"Calibration, {v}, {name}: " + "; ".join(
                f"{row['bin']} n={row['n']}" + (f" {row['predicted']:.1%}->{row['won']:.1%}" if row["n"] else "")
                for row in table))
    return "\n".join(lines)


def parse_variants(specs: Sequence[str]) -> list[Variant]:
    """"app", or name=patch:<p>,k:<games>,pairk:<games>,prev:<weight>"."""
    out = []
    for spec in specs:
        if spec == "app":
            out.append(Variant("app"))
            continue
        name, _, opts = spec.partition("=")
        kw: dict = {}
        for opt in filter(None, opts.split(",")):
            key, _, value = opt.partition(":")
            if key == "patch":
                kw["stats_patch"] = value
            elif key == "k":
                kw["role_prior_games"] = float(value)
            elif key == "pairk":
                kw["pair_k"] = float(value)
            elif key == "prev":
                kw["prev_weight"] = float(value)
            else:
                raise ValueError(f"unknown option {key!r} in {spec!r}")
        out.append(Variant(name, **kw))
    return out


def attach_terms(stats_db: Engine, games: list[Game], variants: Sequence[Variant]) -> None:
    for v in variants:
        terms = engine_terms(stats_db, v)
        for g in games:
            g.terms[v.name] = terms(g)


def main(argv: list[str] | None = None) -> int:
    import json

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stats-db", required=True)
    parser.add_argument("--games-db", required=True)
    parser.add_argument("--after", help="only games started after this date (YYYY-MM-DD or ISO time, UTC)")
    parser.add_argument("--after-brackets", nargs="+", help="apply --after only to these tiers")
    parser.add_argument("--patch", help="only games of this patch")
    parser.add_argument("--brackets", nargs="+")
    parser.add_argument("--variant", nargs="+", default=["app"],
                        help='"app", or name=patch:16.21,k:500,pairk:2000,prev:0.5 (another patch\'s stats, win rate or pair shrinkage, previous patch weight)')
    parser.add_argument("--json", help="also write the report here")
    args = parser.parse_args(argv)
    after = 0
    if args.after:
        when = datetime.fromisoformat(args.after)
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        after = int(when.timestamp() * 1000)
    games = load_games(create_engine(f"sqlite:///{args.games_db}"), after, args.brackets, args.patch, args.after_brackets)
    if not games:
        print("no games to judge")
        return 1
    variants = parse_variants(args.variant)
    attach_terms(create_engine(f"sqlite:///{args.stats_db}"), games, variants)
    report = evaluate(games, [v.name for v in variants])
    print(format_report(report))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
