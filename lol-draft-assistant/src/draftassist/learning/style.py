"""Team style (advisory): classify each logged draft's comp, then suggest the style that has been winning
lately. The style itself is set by hand, and the engine pulls our picks towards it (comp coherence).

Labels are the comp styles of docs/comp-style.md (engine/comp.py `team_comp`): Front to back, Dive, Poke,
or Mixed when no style leads. Before Stage C this used Data Dragon classes and called Front to back
"engage"; stored labels from then are re-classified, and a hand style saved as "engage" reads as
Front to back ("balanced" as Mixed).
"""
from __future__ import annotations

import json

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.db.models import AppSetting, Draft, DraftAction
from draftassist.engine.comp import STYLE_LABELS, ChampionProfiles, team_comp

STYLES = {**STYLE_LABELS, "mixed": "Mixed"}
LEGACY = {"engage": "front_to_back", "balanced": "mixed"}  # labels saved before docs/comp-style.md Stage C
RECENT_GAMES = 20
MIN_STYLE_GAMES = 3
STYLE_K = 5  # shrink each style's win rate towards 50% by this many games
STYLE_KEY = "team_style"


def classify(profiles: ChampionProfiles, picks: list[tuple[int, str | None]]) -> str:
    """A comp's style: the same rule as the Live screen's comp panel."""
    return team_comp(profiles, picks).style


def _draft_style(s: Session, d: Draft, profiles: ChampionProfiles) -> str | None:
    picks = s.exec(select(DraftAction).where(DraftAction.draft_id == d.draft_id, DraftAction.type == "pick",
                                             DraftAction.side == d.side)).all()
    if len(picks) < 5:
        return None
    return classify(profiles, [(a.champ_id, a.role) for a in picks])


def get_hand_style(db: Engine) -> str | None:
    with Session(db) as s:
        row = s.get(AppSetting, STYLE_KEY)
    style = json.loads(row.value).get("style") if row else None
    return LEGACY.get(style, style)


def set_hand_style(db: Engine, style: str | None) -> None:
    if style is not None and style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}")
    with Session(db) as s:
        row = s.get(AppSetting, STYLE_KEY) or AppSetting(key=STYLE_KEY, value="{}")
        row.value = json.dumps({"style": style})
        s.add(row)
        s.commit()


def team_summary(db: Engine, profiles: ChampionProfiles | None = None) -> dict:
    """Recent form, each style's record, the suggested style and the one set by hand."""
    if profiles is None:
        from draftassist.data.profiles import load_profiles

        profiles = load_profiles(db, None)  # styles only: classification doesn't use timing
    with Session(db) as s:
        drafts = s.exec(select(Draft).where(col(Draft.result).in_(("win", "loss")))
                        .order_by(col(Draft.played_at).desc())).all()[:RECENT_GAMES]
        for d in drafts:
            if d.style_tag is None or d.style_tag not in STYLES:
                d.style_tag = _draft_style(s, d, profiles)
                s.add(d)
        s.commit()
        rows = [(d.style_tag, d.result == "win", d.played_at.isoformat()) for d in drafts]
    record = {k: {"label": v, "games": 0, "wins": 0} for k, v in STYLES.items()}
    for style, won, _ in rows:
        if style in record:
            record[style]["games"] += 1
            record[style]["wins"] += won
    for r in record.values():
        r["win_rate_adj"] = round((r["wins"] + 0.5 * STYLE_K) / (r["games"] + STYLE_K), 3)
    eligible = {k: r for k, r in record.items() if r["games"] >= MIN_STYLE_GAMES}
    suggested = max(eligible, key=lambda k: eligible[k]["win_rate_adj"]) if eligible else None
    form = [{"result": "win" if won else "loss", "style": style, "played_at": at} for style, won, at in rows[:10]]
    wins = sum(1 for f in form if f["result"] == "win")
    return {
        "recent": {"games": len(form), "wins": wins, "form": form},
        "styles": record,
        "suggested": suggested,
        "suggested_label": STYLES.get(suggested) if suggested else None,
        "suggestion_note": None if suggested else f"Needs {MIN_STYLE_GAMES}+ finished games of a style among the last {RECENT_GAMES}.",
        "hand_style": get_hand_style(db),
        "choices": STYLES,
    }
