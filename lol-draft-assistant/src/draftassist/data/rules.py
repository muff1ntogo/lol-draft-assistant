"""Rule files for synergy v2 (docs/synergy-v2.md, Stage 2): data/style_rules.yaml (pair style rules) and
data/team_rules.yaml (whole-comp and cross-team rules). Both are in version control and drafted by Claude
for review; the Rules page edits priors and writes the files back (deterministic layout, clean diffs)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from draftassist.config import PROJECT_ROOT
from draftassist.engine.style import PAIRS, StyleRule, parse_style_rules
from draftassist.engine.team import TeamRule, parse_team_rules

STYLE_RULES_FILE = PROJECT_ROOT / "data" / "style_rules.yaml"
TEAM_RULES_FILE = PROJECT_ROOT / "data" / "team_rules.yaml"

STYLE_HEADER = """\
# Pair style rules for the core in-lane pairs (docs/synergy-v2.md, Stage 2a).
# A rule's strength for two champions = product of their trait intensities (0-1). prior_pp is the written
# effect at full strength; the fit to crawled pair data moves it (ridge, prior SD 1pp). Terms: tags (engage,
# disengage, poke, dive, pick, splitpush, enchanter, hypercarry), measured traits (cc, frontline, peel,
# lane_pressure, ranged, dmg_phys, dmg_magic, dmg_true), derived (ranged_long, power_curve_late,
# power_curve_early), `not X`, and products (`a * b`). Drafted by Claude; priors are edited on the Rules page.
"""
TEAM_HEADER = """\
# Whole-comp rules (feature) and cross-team rules (ours x theirs), docs/synergy-v2.md, Stage 2b.
# Features are 0-1: {max: t}, {mean: t}, {sum: t, cap: c}, {lack: t, need: c}, {excess: t, over: x, cap: c},
# {protect: t, by: [..]},
# {all: [f, f]} (product), {special: damage_imbalance | curve_coherence | identity_coherence}.
# prior_pp is the effect of the feature going 0 -> 1; the logistic fit on crawled matches moves it (ridge,
# prior SD 1pp). Drafted by Claude; priors are edited on the Rules page.
"""


def _read(path: Path) -> list:
    if not path.is_file():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def load_style_rules(path: Path = STYLE_RULES_FILE) -> list[StyleRule]:
    return parse_style_rules(_read(path))


def load_team_rules(path: Path = TEAM_RULES_FILE) -> list[TeamRule]:
    return parse_team_rules(_read(path))


def _flow(v) -> str:
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # a JSON string is a valid YAML scalar
    return yaml.safe_dump(v, default_flow_style=True, sort_keys=False, width=1000).strip()


def _pp(x: float) -> str:
    return f"{x:+.1f}" if round(x, 1) == round(x, 6) else f"{x:+g}"


def dump_style_rules(rules: list[StyleRule], path: Path = STYLE_RULES_FILE) -> None:
    lines = [STYLE_HEADER]
    for pair in PAIRS:
        group = [r for r in rules if r.pair == pair]
        if not group:
            continue
        lines.append(f"# ---------- {pair} ----------")
        for r in group:
            d = r.as_dict()
            lines += [f"- pair: {pair}", f"  when: {_flow(d['when'])}", f"  prior_pp: {_pp(r.prior_pp)}",
                      f"  note: {_flow(r.note)}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dump_team_rules(rules: list[TeamRule], path: Path = TEAM_RULES_FILE) -> None:
    lines = [TEAM_HEADER, "# ---------- our comp ----------"]
    for r in (r for r in rules if not r.cross):
        lines += [f"- id: {r.id}", f"  feature: {_flow(dict(r.feature))}", f"  prior_pp: {_pp(r.prior_pp)}",
                  f"  note: {_flow(r.note)}"]
    lines.append("# ---------- ours vs theirs ----------")
    for r in (r for r in rules if r.cross):
        lines += [f"- id: {r.id}", f"  ours: {_flow(dict(r.ours))}", f"  theirs: {_flow(dict(r.theirs))}",
                  f"  prior_pp: {_pp(r.prior_pp)}", f"  note: {_flow(r.note)}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_prior(kind: str, rule_id: str, prior_pp: float, style_path: Path = STYLE_RULES_FILE,
              team_path: Path = TEAM_RULES_FILE) -> None:
    """Change one rule's prior and write its file back. ValueError for an unknown rule or a silly prior."""
    from dataclasses import replace

    if not -10 <= prior_pp <= 10:
        raise ValueError("prior_pp must be between -10 and +10")
    if kind == "style":
        rules = load_style_rules(style_path)
        if rule_id not in {r.id for r in rules}:
            raise ValueError(f"unknown style rule {rule_id}")
        dump_style_rules([replace(r, prior_pp=prior_pp) if r.id == rule_id else r for r in rules], style_path)
    elif kind == "team":
        rules = load_team_rules(team_path)
        if rule_id not in {r.id for r in rules}:
            raise ValueError(f"unknown team rule {rule_id}")
        dump_team_rules([replace(r, prior_pp=prior_pp) if r.id == rule_id else r for r in rules], team_path)
    else:
        raise ValueError("kind must be style or team")
