"""One logged game split into what the draft gave, what the early game changed and what conversion did
(docs/draft-vs-execution.md, Stage 3). `attribute` and `verdict` are pure; `refresh_executions` is the job.

    Draft = P0 - 0.5, Early = P15 - P0, Conversion = Result - P15

Everything is our team's view. The state model reads blue minus red, so the sums run from blue's side and turn around at
the end.

CLI (every logged game again; the fit does this itself, the learning job fills in new games):
    uv run python -m draftassist.learning.execution
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from sqlalchemy import Engine
from sqlmodel import Session, delete, select

from draftassist.data.execution_model import OBJECTIVES, _early, _vision, load_execution_model
from draftassist.data.profiles import load_profiles
from draftassist.db.models import (
    Draft,
    DraftAction,
    GameExecution,
    GameExecutionRole,
    MatchTeamState,
    OwnGameParticipant,
)
from draftassist.engine.execution import Expect
from draftassist.engine.mathx import logit, sigmoid
from draftassist.engine.model import ROLE_LABELS, ROLES

log = logging.getLogger(__name__)

LANES = ("top", "jungle", "mid")
EARLY_CHIPS = (*LANES, "bot", "objectives")  # these add up to Early
AS_EXPECTED, AHEAD, BEHIND, WELL_AHEAD, WELL_BEHIND = "as expected", "ahead", "behind", "well ahead", "well behind"
Z_NOTABLE, Z_LARGE = 1.0, 2.0
OBJECTIVES_LABEL = "Objectives and team"
SHOWN_OBJECTIVES = ("towers", "plates", "dragons", "grubs")  # herald never happens by 15:00 (decision 18)
JUNGLE_OBJECTIVES = ("dragons", "grubs")
LANE_STATS = {"gold": "lane_gold_15", "xp": "lane_xp_15", "cs": "lane_cs_14"}
CONVERSION_NOTES = 2  # what happened after 15:00, at most this many in a verdict's line


@dataclass(frozen=True)
class VerdictRules:
    from_ahead: float = 0.60
    comeback: float = 0.40
    draft_low: float = 0.45
    draft_high: float = 0.55
    early_small: float = 0.10
    lost_early_p15: float = 0.45
    won_early_p15: float = 0.55


RULES = VerdictRules()
VERDICT_PART = {"Lost from ahead": "conversion", "Comeback": "conversion", "Draft-limited": "draft", "Won on the draft": "draft",
                "Lost in the early game": "early", "Won the early game": "early"}


def verdict(p0: float, p15: float, early: float, won: bool, rules: VerdictRules = RULES) -> str:
    """In the doc's order: the first that fits."""
    if p15 >= rules.from_ahead and not won:
        return "Lost from ahead"
    if p15 <= rules.comeback and won:
        return "Comeback"
    if p0 <= rules.draft_low and abs(early) < rules.early_small and not won:
        return "Draft-limited"
    if p0 >= rules.draft_high and abs(early) < rules.early_small and won:
        return "Won on the draft"
    if early <= -rules.early_small and p15 < rules.lost_early_p15:
        return "Lost in the early game"
    if early >= rules.early_small and p15 > rules.won_early_p15:
        return "Won the early game"
    return "Inconclusive"


def label(z: float, lower_is_better: bool = False) -> str:
    good = -z if lower_is_better else z
    if abs(good) < Z_NOTABLE:
        return AS_EXPECTED
    if abs(good) <= Z_LARGE:
        return AHEAD if good > 0 else BEHIND
    return WELL_AHEAD if good > 0 else WELL_BEHIND


@dataclass(frozen=True)
class StatLine:
    stat: str
    actual: float
    expected: float
    sd: float
    z: float
    label: str


def stat_line(stat: str, actual: float, expect: Expect, lower_is_better: bool = False) -> StatLine:
    z = (actual - expect.mean) / expect.sd
    return StatLine(stat, actual, expect.mean, expect.sd, z, label(z, lower_is_better))


@dataclass(frozen=True)
class RoleResult:
    role: str
    share: float  # of Early
    headline: StatLine  # what the label comes from
    stats: tuple[StatLine, ...]
    label: str


@dataclass
class GameFacts:
    side: str
    won: bool
    blue: Mapping[str, int]
    red: Mapping[str, int]
    players: Mapping[int, Mapping[str, float | None]]  # champ -> the timeline columns
    blue_state: Mapping[str, float | None]  # match_team_state columns
    red_state: Mapping[str, float | None]
    early_blue: float
    early_red: float
    brackets: Mapping[str, str]  # our players' tiers
    team_bracket: str


@dataclass(frozen=True)
class Attribution:
    p0: float
    p15: float
    draft: float
    early: float
    conversion: float
    verdict: str
    contributors: tuple[str, ...]
    roles: Mapping[str, RoleResult]
    detail: Mapping

    @property
    def line(self) -> str:
        return f"{self.verdict}: {', '.join(self.contributors)}"


# ---------- one game ----------
def _state_diff(g: GameFacts) -> dict[str, float]:
    return {"gold": g.blue_state["gold_15"] - g.red_state["gold_15"],
            **{n: g.blue_state[f"{n}_15"] - g.red_state[f"{n}_15"] for n in OBJECTIVES}}


def attribute(model, g: GameFacts, p0: float, rules: VerdictRules = RULES) -> Attribution:
    """`model`: data.execution_model.ExecutionModel. `p0`: our calibrated chance at lock-in."""
    is_blue = g.side == "blue"
    state = _state_diff(g)
    offset = logit(p0 if is_blue else 1 - p0)

    def p_ours(without: float = 0.0) -> float:
        """Our chance at 15:00, with this much of blue's gold taken away."""
        p = model.p15(offset, {**state, "gold": state["gold"] - without}, g.early_blue, g.early_red, g.blue, g.red, g.team_bracket)
        return p if is_blue else 1 - p

    p15 = p_ours()
    gold = {r: g.players[g.blue[r]]["lane_gold_15"] - model.lane("gold", r, g.blue[r], g.red[r], g.brackets.get(r)).mean
            for r in LANES}
    half_bot = model.bot("gold", g.blue["adc"], g.blue["support"], g.red["adc"], g.red["support"], g.team_bracket).mean / 2
    adc_gold = g.players[g.blue["adc"]]["lane_gold_15"] - half_bot
    sup_gold = g.players[g.blue["support"]]["lane_gold_15"] - half_bot

    shares = {r: p15 - p_ours(gold[r]) for r in LANES}
    no_bot = p_ours(adc_gold + sup_gold)
    shares["bot"] = p15 - no_bot
    # The two bot players, each averaged over being taken out first and second, so together they make the bot chip exactly
    no_adc, no_sup = p_ours(adc_gold), p_ours(sup_gold)
    shares["adc"] = ((p15 - no_adc) + (no_sup - no_bot)) / 2
    shares["support"] = ((p15 - no_sup) + (no_adc - no_bot)) / 2
    early = p15 - p0
    shares["objectives"] = early - sum(shares[r] for r in (*LANES, "bot"))

    roles = _roles(model, g, shares, state)
    conversion = (1.0 if g.won else 0.0) - p15
    parts = {"draft": p0 - 0.5, "early": early, "conversion": conversion}
    detail = _conversion_detail(model, g, state)
    called = verdict(p0, p15, early, g.won, rules)
    return Attribution(p0, p15, parts["draft"], early, conversion, called, _contributors(called, parts, roles, detail), roles, detail)


def _roles(model, g: GameFacts, shares: Mapping[str, float], state: Mapping[str, float]) -> dict[str, RoleResult]:
    is_blue = g.side == "blue"
    ours, theirs = (g.blue, g.red) if is_blue else (g.red, g.blue)
    sign = 1 if is_blue else -1

    def lane_lines(role: str, targets=LANE_STATS) -> list[StatLine]:
        me, row, bracket = ours[role], g.players[ours[role]], g.brackets.get(role)
        return [stat_line(t, row[col], model.lane(t, role, me, theirs[role], bracket))
                for t, col in targets.items() if row.get(col) is not None]

    def death_lines(role: str) -> list[StatLine]:
        me, row, bracket = ours[role], g.players[ours[role]], g.brackets.get(role)
        out = []
        if row.get("deaths_solo_14") is not None:
            out.append(stat_line("deaths_solo", row["deaths_solo_14"], model.deaths_solo(role, me, theirs[role], bracket), True))
        if row.get("deaths_ganked_14") is not None:
            out.append(stat_line("deaths_ganked", row["deaths_ganked_14"], model.deaths_ganked(role, me, theirs["jungle"], bracket), True))
        return out

    def objective_lines(names) -> list[StatLine]:
        out = []
        for name in names:
            e = model.objective(name, g.blue["jungle"], g.red["jungle"], g.early_blue, g.early_red, g.team_bracket)
            out.append(stat_line(name, sign * state[name], Expect(sign * e.mean, e.sd)))
        return out

    def bot_lines() -> list[StatLine]:
        out = []
        for target in ("gold", "xp"):
            column = LANE_STATS[target]
            adc, sup = g.players[ours["adc"]].get(column), g.players[ours["support"]].get(column)
            if adc is not None and sup is not None:
                out.append(stat_line(target, adc + sup, model.bot(target, ours["adc"], ours["support"], theirs["adc"], theirs["support"], g.team_bracket)))
        return out

    def by_name(lines: list[StatLine], name: str) -> StatLine:
        return next(s for s in lines if s.stat == name)

    jungler, row = ours["jungle"], g.players[ours["jungle"]]
    jungle = []
    if row.get("jungle_cs_15") is not None:
        jungle.append(stat_line("jungle_cs", row["jungle_cs_15"], model.jungle_cs(jungler, g.brackets.get("jungle"))))
    jungle += objective_lines(JUNGLE_OBJECTIVES)
    if row.get("kp_15") is not None:
        jungle.append(stat_line("kp", row["kp_15"], model.kp(jungler, g.brackets.get("jungle"))))
    # Its share of Early comes from its gold, so its label does too (decision 21), though gold isn't one of its shown stats
    jungle_gold = lane_lines("jungle", {"gold": LANE_STATS["gold"]})[0]

    support = lane_lines("support")
    sup_row = g.players[ours["support"]]
    vision = _vision(sup_row)
    if vision is not None:
        support.append(stat_line("vision", vision, model.vision(ours["support"], g.brackets.get("support"))))
    support += death_lines("support")

    objectives = objective_lines(SHOWN_OBJECTIVES)
    lines = {"top": lane_lines("top") + death_lines("top"), "jungle": jungle, "mid": lane_lines("mid") + death_lines("mid"),
             "bot": bot_lines(), "adc": lane_lines("adc") + death_lines("adc"), "support": support, "objectives": objectives}
    headline = {r: by_name(lines[r], "gold") for r in ("top", "mid", "bot", "adc", "support")}
    headline["jungle"] = jungle_gold
    # The objectives chip is what's left of Early, so it speaks only for the objectives that went the way that part did
    agreeing = [s for s in objectives if s.z * shares["objectives"] > 0 and s.label != AS_EXPECTED]
    headline["objectives"] = max(agreeing or objectives, key=lambda s: abs(s.z))
    labels = {r: headline[r].label for r in lines}
    labels["objectives"] = headline["objectives"].label if agreeing else AS_EXPECTED
    return {r: RoleResult(r, shares[r], headline[r], tuple(lines[r]), labels[r]) for r in lines}


def _conversion_detail(model, g: GameFacts, state: Mapping[str, float]) -> dict:
    is_blue = g.side == "blue"
    ours, theirs = (g.blue_state, g.red_state) if is_blue else (g.red_state, g.blue_state)
    detail: dict = {name: [ours.get(name) or 0.0, theirs.get(name) or 0.0]
                    for name in ("deaths_while_ahead", "barons", "dragons_after_15", "towers_after_15")}
    gold_25 = None
    if g.blue_state.get("gold_25") is not None and g.red_state.get("gold_25") is not None:
        sign = 1 if is_blue else -1
        e = model.gold_25(state["gold"], g.early_blue, g.early_red, g.team_bracket)
        gold_25 = asdict(stat_line("gold_25", sign * (g.blue_state["gold_25"] - g.red_state["gold_25"]), Expect(sign * e.mean, e.sd)))
    detail["gold_25"] = gold_25
    return detail


# ---------- the verdict's line ----------
def _pp(x: float) -> str:
    return f"{100 * x:+.0f}".replace("-", "−") + "pp"


def _count(n: float, one: str, many: str | None = None) -> str:
    n = int(n)
    return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"


def _conversion_notes(conversion: float, detail: Mapping) -> list[str]:
    """What the conversion part points at, from whichever side it went for."""
    (own_deaths, enemy_deaths), (barons, enemy_barons) = detail["deaths_while_ahead"], detail["barons"]
    dragons = detail["dragons_after_15"][0] - detail["dragons_after_15"][1]
    towers = detail["towers_after_15"][0] - detail["towers_after_15"][1]
    notes = []
    if conversion < 0:
        if own_deaths > 0:
            notes.append(_count(own_deaths, "death") + " while ahead")
        if enemy_barons > barons:
            n = enemy_barons - barons
            notes.append("Baron lost" if n == 1 else f"{int(n)} Barons lost")
        if dragons < 0:
            notes.append(f"{_count(-dragons, 'dragon')} behind after 15")
        if towers < 0:
            notes.append(f"{_count(-towers, 'tower')} behind after 15")
    else:
        if enemy_deaths > 0:
            notes.append("their " + _count(enemy_deaths, "death") + " while ahead")
        if barons > enemy_barons:
            n = barons - enemy_barons
            notes.append("Baron taken" if n == 1 else f"{int(n)} Barons taken")
        if dragons > 0:
            notes.append(f"{_count(dragons, 'dragon')} ahead after 15")
        if towers > 0:
            notes.append(f"{_count(towers, 'tower')} ahead after 15")
    return notes[:CONVERSION_NOTES]


def _early_note(early: float, roles: Mapping[str, RoleResult]) -> str | None:
    top = max(EARLY_CHIPS, key=lambda r: roles[r].share if early > 0 else -roles[r].share)
    if top == "objectives":
        return OBJECTIVES_LABEL
    if roles[top].label == AS_EXPECTED:
        return None
    return f"{'Bot' if top == 'bot' else ROLE_LABELS[top]} {roles[top].label}"


def _contributors(called: str, parts: Mapping[str, float], roles: Mapping[str, RoleResult], detail: Mapping) -> tuple[str, ...]:
    """The part the verdict is about, then the largest of the rest. Inconclusive is about none, so the two largest."""
    named = VERDICT_PART.get(called)
    out = []
    for name in sorted(parts, key=lambda n: (n != named, -abs(parts[n])))[:2]:
        note = None
        if name == "early":
            note = _early_note(parts[name], roles)
        elif name == "conversion":
            note = ", ".join(_conversion_notes(parts[name], detail)) or None
        out.append(f"{_pp(parts[name])} {name}" + (f" ({note})" if note else ""))
    return tuple(out)


# ---------- the draft's chance ----------
class DraftScorer:
    """P0: the engine's draft and our comfort, scaled together as crawled games say the draft should be (decision 26)."""

    def __init__(self, engine, scale: float):
        self.engine, self.scale = engine, scale
        self.brackets = engine.team.brackets
        self.team_bracket = engine.cfg.enemy_bracket

    @classmethod
    def load(cls, db: Engine, scale: float) -> DraftScorer:
        from draftassist.api.main import with_synergy_v2
        from draftassist.data.provider import load_engine_inputs
        from draftassist.data.settings_store import engine_config
        from draftassist.engine.engine import DraftEngine

        inputs = load_engine_inputs(db)
        return cls(DraftEngine(with_synergy_v2(db, inputs), inputs.team, engine_config(db)), scale)

    def p0(self, ours: Mapping[str, int], theirs: Mapping[str, int]) -> float:
        return sigmoid(self.scale * (self.engine.draft_logodds(ours, theirs) + self.engine.draft_comfort(ours)))

    def roles(self, draft: Draft, actions: list[DraftAction]) -> tuple[dict[str, int], dict[str, int]]:
        """With no timeline to read positions from: ours as the client gave them, else assigned, as theirs are."""
        from draftassist.engine.formats import get_format
        from draftassist.engine.state import DraftState

        champs = tuple(a.champ_id for a in sorted(actions, key=lambda a: a.seq))
        ev = self.engine.evaluate(DraftState(get_format(draft.draft_format or "tournament"), draft.side, champs))
        given = {a.role: a.champ_id for a in actions if a.type == "pick" and a.side == draft.side and a.role in ROLES}
        ours = given if len(given) == len(ROLES) else {r: c for c, r in ev.our_roles.items()}
        return ours, {r: c for c, r in ev.their_roles.items()}


# ---------- the job ----------
_REQUIRED_STATE = ("gold_15", *(f"{n}_15" for n in OBJECTIVES))


def _teams(parts: list[OwnGameParticipant]) -> tuple[dict[str, int], dict[str, int]] | None:
    """Both teams by position, if the match gave all ten."""
    blue = {p.role: p.champ_id for p in parts if p.team_id == 100 and p.role in ROLES}
    red = {p.role: p.champ_id for p in parts if p.team_id == 200 and p.role in ROLES}
    return (blue, red) if len(blue) == len(ROLES) == len(red) else None


def _complete(parts: list[OwnGameParticipant], states: Mapping[int, MatchTeamState]) -> bool:
    if _teams(parts) is None or len(parts) != 2 * len(ROLES) or any(p.lane_gold_15 is None for p in parts):
        return False
    return all((t := states.get(team)) is not None and all(getattr(t, c) is not None for c in _REQUIRED_STATE) for team in (100, 200))


def _drop(s: Session, draft_id: int) -> None:
    s.exec(delete(GameExecutionRole).where(GameExecutionRole.draft_id == draft_id))
    s.exec(delete(GameExecution).where(GameExecution.draft_id == draft_id))


def _store(s: Session, draft_id: int, window: str, p0: float, a: Attribution | None) -> None:
    _drop(s, draft_id)
    if a is None:
        s.add(GameExecution(draft_id=draft_id, window=window, p0=p0, draft=p0 - 0.5))
        return
    s.add(GameExecution(draft_id=draft_id, window=window, p0=a.p0, p15=a.p15, draft=a.draft, early=a.early, conversion=a.conversion,
                        verdict=a.verdict, contributors_json=json.dumps(a.contributors), detail_json=json.dumps(a.detail)))
    s.flush()
    for role, r in a.roles.items():
        s.add(GameExecutionRole(draft_id=draft_id, role=role, share=r.share, label=r.label, headline_json=json.dumps(asdict(r.headline)),
                                stats_json=json.dumps([asdict(x) for x in r.stats])))


def refresh_executions(db: Engine, only_missing: bool = False, scorer=None) -> int:
    """Every logged game with a result and a full draft, or with `only_missing` just new games and timelines that have
    arrived since. Returns how many were written. Nothing without a fit."""
    model = load_execution_model(db)
    if model is None:
        return 0
    with Session(db) as s:
        drafts = s.exec(select(Draft).where(Draft.result != None)).all()  # noqa: E711
        actions: dict[int, list[DraftAction]] = {}
        for a in s.exec(select(DraftAction)).all():
            actions.setdefault(a.draft_id, []).append(a)
        parts: dict[int, list[OwnGameParticipant]] = {}
        for p in s.exec(select(OwnGameParticipant)).all():
            parts.setdefault(p.draft_id, []).append(p)
        states: dict[str, dict[int, MatchTeamState]] = {}
        match_ids = {p.match_id for ps in parts.values() for p in ps}
        for t in s.exec(select(MatchTeamState).where(MatchTeamState.match_id.in_(match_ids))).all() if match_ids else ():
            states.setdefault(t.match_id, {})[t.team_id] = t
        done = {r.draft_id: r.p15 is not None for r in s.exec(select(GameExecution)).all()}

        written, profiles = 0, None
        for draft in drafts:
            if draft.result not in ("win", "loss"):  # a remake has nothing to split
                continue
            game_actions = actions.get(draft.draft_id, [])
            picks = [a for a in game_actions if a.type == "pick"]
            if sum(a.side == draft.side for a in picks) != len(ROLES) or len(picks) != 2 * len(ROLES):
                continue
            game_parts = parts.get(draft.draft_id, [])
            game_states = states.get(game_parts[0].match_id, {}) if game_parts else {}
            complete = _complete(game_parts, game_states)
            if only_missing and draft.draft_id in done and (done[draft.draft_id] or not complete):
                continue
            if scorer is None:
                scorer = DraftScorer.load(db, model.draft_scale)
            teams = _teams(game_parts)
            if teams is not None:
                blue, red = teams
                ours, theirs = (blue, red) if draft.side == "blue" else (red, blue)
            else:
                ours, theirs = scorer.roles(draft, game_actions)
                blue, red = (ours, theirs) if draft.side == "blue" else (theirs, ours)
            p0 = scorer.p0(ours, theirs)
            result = None
            if complete:
                profiles = profiles if profiles is not None else load_profiles(db, model.window)
                facts = GameFacts(
                    draft.side, draft.result == "win", blue, red, {p.champ_id: p.model_dump() for p in game_parts},
                    game_states[100].model_dump(), game_states[200].model_dump(), _early(profiles, blue), _early(profiles, red),
                    scorer.brackets, scorer.team_bracket)
                result = attribute(model, facts, p0)
            _store(s, draft.draft_id, model.window, p0, result)
            written += 1
        s.commit()
    return written


def main() -> int:
    from draftassist.config import load_settings
    from draftassist.db.session import init_db

    db = init_db(load_settings().db_path)
    print(f"{refresh_executions(db)} games attributed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
