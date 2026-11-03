"""Comp type and team-fight style (docs/comp-style.md). Pure, no I/O.

Power curve, per champion and role, from crawled games bucketed by length:
    curve = logit(WR in games > 32 min) - logit(WR in games < 25 min)
each bucket shrunk towards the champion's own win rate p (k = 50 games), so a champion that is simply
strong reads 0, and thin data reads close to 0. Negative = early, positive = late.

Team-fight style per champion: weights over (front to back, dive, poke) summing to 1, plus a front-line
flag (data/profiles.py: a reviewed seed file).
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

SHORT_S = 25 * 60  # games shorter than this are "short"
LONG_S = 32 * 60  # longer than this are "long"
CURVE_K = 50  # shrinkage games per bucket, towards the champion's own win rate
CURVE_MIN_GAMES = 50  # short + long games needed for a timing label
# |curve| beyond this (log-odds, ~3.7pp each way) is early / late. Patch 16.21 (230 labelled champion-roles,
# sd 0.22): 0.10 labelled two thirds, mostly noise at 100-200 games; 0.15 labels about half.
TIMING_THRESHOLD = 0.15

STYLES = ("front_to_back", "dive", "poke")
STYLE_LABELS = {"front_to_back": "Front to back", "dive": "Dive", "poke": "Poke"}
TIMING_LABELS = {"early": "Early", "mid": "Mid", "late": "Late"}


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def power_curve(wins_short: float, games_short: float, wins_long: float, games_long: float, p: float,
                k: float = CURVE_K) -> float:
    short = (wins_short + k * p) / (games_short + k)
    long = (wins_long + k * p) / (games_long + k)
    return _logit(long) - _logit(short)


def timing_label(curve: float | None, threshold: float = TIMING_THRESHOLD) -> str | None:
    if curve is None:
        return None
    return "early" if curve < -threshold else "late" if curve > threshold else "mid"


@dataclass(frozen=True)
class TimingCurve:
    curve: float
    games: int  # short + long games behind it

    @property
    def label(self) -> str | None:
        return timing_label(self.curve) if self.games >= CURVE_MIN_GAMES else None


@dataclass(frozen=True)
class ChampStyle:
    weights: tuple[float, float, float]  # front to back, dive, poke
    frontline: bool
    source: str  # seed | user | classes (Data Dragon classes: champion missing from the seed)
    reviewed: bool
    unsure: bool  # seeded mostly from classes: review first

    def as_dict(self) -> dict[str, float]:
        return dict(zip(STYLES, self.weights))


@dataclass(frozen=True)
class ChampionProfiles:
    """Every champion's style (reviewed seed) and power curves per role (data/profiles.py loads it)."""
    styles: Mapping[int, ChampStyle]
    curves: Mapping[tuple[int, str], TimingCurve]

    def style(self, champ: int) -> ChampStyle | None:
        return self.styles.get(champ)

    def curve(self, champ: int, role: str | None) -> TimingCurve | None:
        """The curve in `role`; with no role (e.g. an enemy whose role is unknown), the role with most games."""
        if role is not None:
            return self.curves.get((champ, role))
        found = [tc for (c, _), tc in self.curves.items() if c == champ]
        return max(found, key=lambda tc: tc.games, default=None)


EMPTY_PROFILES = ChampionProfiles({}, {})

# ---------- badges and team comp (Live screen) ----------
MAIN_STYLE = 0.5  # a champion "is" a style (badge, contributor) at this weight or more
STYLE_SHARE = 0.45  # a team's style is named at this share of its picks' weights...
STYLE_CONTRIBUTORS = 2  # ...with at least this many champions of that style
COMP_TIMING_LABELS = {"early": "Early", "mid": "Mix", "late": "Late"}
FULL_CARDS, NARROW_CARDS = 5, 3  # suggestion cards while the comp is open / converging


@dataclass(frozen=True)
class Badge:
    timing: str | None  # early | mid | late; None = not enough games
    styles: tuple[str, ...]  # styles with weight >= MAIN_STYLE: one, two for a 0.5 / 0.5 champion, or none
    frontline: bool

    @property
    def style(self) -> str | None:
        """The champion's single main style, None when split or spread."""
        return self.styles[0] if len(self.styles) == 1 else None

    def text(self) -> str:
        parts = [TIMING_LABELS[self.timing]] if self.timing in ("early", "late") else []
        if self.styles:
            parts.append(" / ".join(STYLE_LABELS[s] for s in self.styles))
        if self.frontline:
            parts.append("Front-line")
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return {"timing": self.timing, "style": self.style, "styles": list(self.styles),
                "frontline": self.frontline, "text": self.text()}


def badge(profiles: ChampionProfiles, champ: int, role: str | None) -> Badge:
    st = profiles.style(champ)
    tc = profiles.curve(champ, role)
    styles = tuple(k for k, w in zip(STYLES, st.weights) if w >= MAIN_STYLE) if st else ()
    return Badge(tc.label if tc else None, styles, bool(st and st.frontline))


@dataclass(frozen=True)
class TeamComp:
    picks: int
    timing: str | None  # early | mid | late from the mean curve of picks with a label; None = no data yet
    curve: float | None
    shares: Mapping[str, float]  # style weights summed over picks, as shares (0 with no picks)
    style: str  # a STYLES key, or "mixed" until one leads
    frontliners: int

    @property
    def confidence(self) -> float:
        return self.picks / 5

    def as_dict(self) -> dict:
        return {"picks": self.picks, "confidence": self.confidence, "timing": self.timing,
                "timing_label": COMP_TIMING_LABELS.get(self.timing or "", "—"),
                "curve": None if self.curve is None else round(self.curve, 3),
                "shares": {k: round(v, 3) for k, v in self.shares.items()}, "style": self.style,
                "style_label": STYLE_LABELS.get(self.style, "Mixed"), "frontliners": self.frontliners}


def team_comp(profiles: ChampionProfiles, picks: list[tuple[int, str | None]]) -> TeamComp:
    """picks: (champion, role or None). Converges as picks are added: a style needs STYLE_SHARE of the
    weight and STYLE_CONTRIBUTORS champions of it; Front to back also needs a front-liner."""
    curves = [tc.curve for c, r in picks if (tc := profiles.curve(c, r)) is not None and tc.label is not None]
    curve = sum(curves) / len(curves) if curves else None
    styles = [st for c, _ in picks if (st := profiles.style(c)) is not None]
    totals = [sum(st.weights[i] for st in styles) for i in range(len(STYLES))]
    shares = {k: (t / len(styles) if styles else 0.0) for k, t in zip(STYLES, totals)}
    frontliners = sum(st.frontline for st in styles)
    style = "mixed"
    if styles:
        top = max(STYLES, key=lambda k: shares[k])
        i = STYLES.index(top)
        contributors = sum(st.weights[i] >= MAIN_STYLE for st in styles)
        if (shares[top] >= STYLE_SHARE and contributors >= STYLE_CONTRIBUTORS
                and (top != "front_to_back" or frontliners > 0)):
            style = top
    return TeamComp(len(picks), timing_label(curve), curve, shares, style, frontliners)


def comp_effect(profiles: ChampionProfiles, picks: list[tuple[int, str | None]], champ: int,
                role: str | None) -> list[str]:
    """What adding `champ` does to a team's comp, for suggestion cards: "completes Dive", "pushes Late"."""
    before = team_comp(profiles, picks)
    after = team_comp(profiles, [*picks, (champ, role)])
    b = badge(profiles, champ, role)
    out = []
    if after.style != "mixed" and after.style != before.style:
        out.append(("completes " if before.style == "mixed" else "shifts to ") + STYLE_LABELS[after.style])
    elif after.style != "mixed" and after.style in b.styles:
        out.append("fits " + STYLE_LABELS[after.style])
    if b.timing in ("early", "late"):
        out.append("pushes " + TIMING_LABELS[b.timing])
    return out


def card_count(step_kind: str | None, phase: int | None, our_picks: int) -> int:
    """Suggestion cards: 5 while our comp is open (tournament ban phase 1, our picks 1-3), 3 once it
    converges (ban phase 2, our picks 4-5)."""
    if step_kind == "ban":
        return FULL_CARDS if phase == 1 else NARROW_CARDS
    return FULL_CARDS if our_picks < 3 else NARROW_CARDS
