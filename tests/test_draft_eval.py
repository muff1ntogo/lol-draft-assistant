"""The draft model check (data/draft_eval.py) and the parts it splits a draft into (DraftEngine.draft_terms)."""
from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import create_engine, text

from draftassist.data.draft_eval import (
    Game,
    auc,
    brier,
    evaluate,
    fit_logistic,
    load_games,
    log_loss,
    out_of_fold,
    parse_variants,
)
from draftassist.engine.model import ROLES


def _engine_and_comps():
    from draftassist.engine.engine import DraftEngine
    from draftassist.engine.model import EngineConfig
    from draftassist.engine.simulate import synthetic_data

    data, team = synthetic_data()
    meta = data.brackets[EngineConfig().enemy_bracket].meta
    ranked = {r: sorted((c for c in meta if r in meta[c]), key=lambda c: -meta[c][r].pick_rate) for r in ROLES}
    used: set[int] = set()
    blue, red = {}, {}
    for role in ROLES:
        free = [c for c in ranked[role] if c not in used]
        blue[role], red[role] = free[0], free[1]
        used |= {free[0], free[1]}
    from draftassist.engine.model import OurTeam

    one_bracket = OurTeam(team.pools, dict.fromkeys(ROLES, EngineConfig().enemy_bracket))  # as crawled games are scored
    return DraftEngine(data, one_bracket, EngineConfig(alpha=1.5, beta=0.3)), blue, red


def test_the_parts_add_up_to_the_draft_log_odds():
    engine, blue, red = _engine_and_comps()
    t = engine.draft_terms(blue, red)
    alpha = engine.cfg.alpha
    assert engine.draft_logodds(blue, red) == pytest.approx(
        (alpha + 1) / 2 * t["meta"] + t["lane"] + t["syn"] + t["team"] + t["damage"])
    back = engine.draft_terms(red, blue)
    for k, v in t.items():
        assert back[k] == pytest.approx(-v, abs=1e-9)  # no home side in any part


def test_scores_on_known_values():
    y = np.array([1, 0, 1, 0], dtype=float)
    assert brier(np.full(4, 0.5), y) == pytest.approx(0.25)
    assert log_loss(np.full(4, 0.5), y) == pytest.approx(np.log(2))
    assert auc(np.array([0.9, 0.1, 0.8, 0.2]), y) == 1.0
    assert auc(np.array([0.1, 0.9, 0.2, 0.8]), y) == 0.0
    assert auc(np.full(4, 0.5), y) == 0.5


def test_logistic_recovers_planted_weights():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(40_000, 2))
    y = (rng.random(40_000) < 1 / (1 + np.exp(-(0.1 + 0.5 * x[:, 0] - 0.2 * x[:, 1])))).astype(float)
    w = fit_logistic(x, y, ridge=0.0)
    assert w == pytest.approx([0.1, 0.5, -0.2], abs=0.03)


def test_out_of_fold_never_sees_its_own_game():
    # One feature that is exactly the label in fold 0 and 0 elsewhere: an in-sample fit would score fold 0 perfectly
    rng = np.random.default_rng(2)
    n = 5000
    folds = np.arange(n) % 5
    y = (rng.random(n) < 0.5).astype(float)
    x = np.where(folds == 0, y * 2 - 1, 0.0)[:, None]
    p, w = out_of_fold(x, y, folds)
    in_sample = 1 / (1 + np.exp(-(w[0] + w[1] * x[folds == 0, 0])))
    assert brier(in_sample, y[folds == 0]) < 0.05  # fitted on every game, fold 0 looks easy...
    assert brier(p[folds == 0], y[folds == 0]) > 0.24  # ...out of fold it is the coin flip it really is


def _game(i, terms, won):
    return Game(f"EUW1_{i}", "gold", dict.fromkeys(ROLES, 1), dict.fromkeys(ROLES, 2), won, i % 5, {"v": terms})


def test_evaluate_finds_an_overconfident_model_and_the_part_that_carries_signal():
    rng = np.random.default_rng(3)
    games = []
    for i in range(20_000):
        meta, syn = rng.normal(0, 0.4), rng.normal(0, 0.3)  # meta is real at half its size; syn is noise
        won = bool(rng.random() < 1 / (1 + np.exp(-0.5 * meta)))
        terms = {"meta": meta, "meta_on": meta, "off_role": 0.0, "lane": 0.0, "syn": syn, "team": 0.0,
                 "current": meta + syn}
        games.append(_game(i, terms, won))
    r = evaluate(games, ["v"])
    rows = r["rows"]
    assert rows["v: one scale"]["brier"] < rows["v: as the app scores it"]["brier"]
    assert rows["v: side + weight per part"]["brier"] < rows["v: one scale"]["brier"]
    w = rows["v: side + weight per part"]["weights"]
    assert w["meta_on"] == pytest.approx(0.5, abs=0.06)
    assert abs(w["syn"]) < 0.06


def test_load_games_keeps_complete_games_after_the_cutoff(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'g.sqlite'}")
    with db.begin() as c:
        c.execute(text("CREATE TABLE crawled_matches (match_id TEXT, patch TEXT, bracket TEXT, game_start_ms INT)"))
        c.execute(text("CREATE TABLE match_participants (match_id TEXT, team_id INT, role TEXT, champ_id INT, win INT)"))
        for m, start, roles in (("A", 2000, ROLES), ("B", 500, ROLES), ("C", 2000, ROLES[:4])):
            c.execute(text("INSERT INTO crawled_matches VALUES (:m, '16.22', 'gold', :s)"), {"m": m, "s": start})
            for team, base, win in ((100, 1, 1), (200, 50, 0)):
                for k, r in enumerate(roles):
                    c.execute(text("INSERT INTO match_participants VALUES (:m, :t, :r, :c, :w)"),
                              {"m": m, "t": team, "r": r, "c": base + k, "w": win})
    games = load_games(db, after_ms=1000)
    assert [g.match_id for g in games] == ["A"]
    assert games[0].blue_won and games[0].blue["top"] == 1 and games[0].red["support"] == 54
    assert [g.match_id for g in load_games(db, after_ms=1000, after_brackets=["silver"])] == ["A", "B"]
    assert [g.match_id for g in load_games(db, after_ms=1000, after_brackets=["gold"])] == ["A"]


def test_variants_parse():
    a, b = parse_variants(["app", "p21=patch:16.21,k:500,prev:0"])
    assert a.stats_patch is None and a.role_prior_games is None and a.prev_weight is None
    assert (b.name, b.stats_patch, b.role_prior_games, b.prev_weight) == ("p21", "16.21", 500.0, 0.0)
    with pytest.raises(ValueError):
        parse_variants(["x=foo:1"])
