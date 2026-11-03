"""JSON for synergy v2 (docs/synergy-v2.md, Stage 3): results in pp (25 x log-odds), champions as cards."""
from __future__ import annotations

from collections.abc import Callable

from draftassist.engine.style import StyleModel
from draftassist.engine.synergy_v2 import CompResult, PairResult, Suggestion
from draftassist.engine.team import IDENTITY_LABELS


def pp(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(25 * x, nd)


def rules_json(rules) -> list[dict]:
    return [{"id": r.id, "note": r.note, "pp": pp(v), "label": StyleModel.label((r, v))} for r, v in rules]


def pair_json(p: PairResult, card: Callable[[int], dict]) -> dict:
    return {"roles": [p.role_a, p.role_b], "champs": [card(p.a), card(p.b)], "core": p.core,
            "stat_pp": pp(p.stat), "style_pp": pp(p.style), "lane_pp": pp(p.lane),
            "overall_pp": pp(p.contribution), "games": p.games, "confidence": round(p.confidence, 3),
            "low_data": p.low_data, "rules": rules_json(p.rules)}


def team_json(r: CompResult) -> dict:
    vs = None
    if r.team_vs is not None:
        vs = {"total_pp": pp(r.team_vs.total), "own_pp": pp(r.team_vs.own), "cross_pp": pp(r.team_vs.cross),
              "rules": [{"id": rule.id, "note": rule.note, "cross": rule.cross, "pp": pp(v)}
                        for rule, v in r.team_vs.contributions[:3]]}
    ident = r.identity
    return {
        "stat_total_pp": pp(r.stat_total), "style_total_pp": pp(r.style_total), "team_pp": pp(r.team),
        "team_weighted_pp": pp(r.team_weighted), "overall_pp": pp(r.overall), "show_team": r.show_team,
        "team_vs": vs,
        "identity": {"label": ident.label, "name": IDENTITY_LABELS.get(ident.label),
                     "coherence": round(ident.coherence, 3)},
        "damage": {k: round(v, 4) for k, v in r.damage.items()},
        "bars": {k: round(v, 3) for k, v in r.bars.items()},
        "team_rules": [{"id": rule.id, "note": rule.note, "pp": pp(v)} for rule, v in r.team_rules],
    }


def suggestion_json(s: Suggestion, card: Callable[[int], dict], tier: str | None = None) -> dict:
    r = s.result
    return {"champ": card(s.champ), "role": s.role, "tier": tier, "gain_pp": pp(s.gain),
            "overall_pp": pp(r.overall), "stat_total_pp": pp(r.stat_total), "style_total_pp": pp(r.style_total),
            "team_weighted_pp": pp(r.team_weighted)}


def evaluation_json(result: CompResult, card: Callable[[int], dict], suggestions: dict | None = None,
                    tier: Callable[[int, str], str | None] = lambda c, r: None) -> dict:
    return {"pairs": [pair_json(p, card) for p in result.pairs], "team": team_json(result),
            "suggestions": {role: [suggestion_json(s, card, tier(s.champ, role)) for s in rows]
                            for role, rows in (suggestions or {}).items()}}
