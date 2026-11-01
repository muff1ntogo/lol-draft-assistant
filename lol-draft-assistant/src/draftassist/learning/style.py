"""Team style (v2, advisory): classify each logged draft's comp from the champions' classes,
then suggest the style that has been winning lately. The style itself is set by hand.

Styles from Data Dragon classes (Tank, Fighter, Assassin, Mage, Marksman, Support):
    engage = Tanks + half the Fighters    (front-to-back, hard engage)
    dive   = Assassins + half the Fighters (pick, dive the backline)
    poke   = Mages + Marksmen beyond the one ADC (poke, scaling, siege)
A comp scoring under 1.5 in every style is "balanced".
"""
from __future__ import annotations

import json

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.db.models import AppSetting, Champion, Draft, DraftAction

STYLES = {"engage": "Engage / front-to-back", "dive": "Dive / pick", "poke": "Poke / scaling",
          "balanced": "Balanced"}
RECENT_GAMES = 20
MIN_STYLE_GAMES = 3
STYLE_K = 5  # shrink each style's win rate towards 50% by this many games
STYLE_KEY = "team_style"


def classify(tags: list[list[str]]) -> str:
    count = lambda t: sum(t in champ for champ in tags)  # noqa: E731
    scores = {"engage": count("Tank") + 0.5 * count("Fighter"),
              "dive": count("Assassin") + 0.5 * count("Fighter"),
              "poke": count("Mage") + max(count("Marksman") - 1, 0)}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 1.5 else "balanced"


def _draft_style(s: Session, d: Draft, tags: dict[int, list[str]]) -> str | None:
    picks = s.exec(select(DraftAction).where(DraftAction.draft_id == d.draft_id, DraftAction.type == "pick",
                                             DraftAction.side == d.side)).all()
    if len(picks) < 5:
        return None
    return classify([tags.get(a.champ_id, []) for a in picks])


def get_hand_style(db: Engine) -> str | None:
    with Session(db) as s:
        row = s.get(AppSetting, STYLE_KEY)
    return json.loads(row.value).get("style") if row else None


def set_hand_style(db: Engine, style: str | None) -> None:
    if style is not None and style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}")
    with Session(db) as s:
        row = s.get(AppSetting, STYLE_KEY) or AppSetting(key=STYLE_KEY, value="{}")
        row.value = json.dumps({"style": style})
        s.add(row)
        s.commit()


def team_summary(db: Engine) -> dict:
    """Recent form, each style's record, the suggested style and the one set by hand."""
    with Session(db) as s:
        tags = {c.champ_id: c.tags or [] for c in s.exec(select(Champion)).all()}
        drafts = s.exec(select(Draft).where(col(Draft.result).in_(("win", "loss")))
                        .order_by(col(Draft.played_at).desc())).all()[:RECENT_GAMES]
        for d in drafts:
            if d.style_tag is None:
                d.style_tag = _draft_style(s, d, tags)
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
