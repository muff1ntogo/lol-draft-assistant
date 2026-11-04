"""History in a tournament broadcast style (docs/history-broadcast-ui.md): the game list and one
game's recap: bans by phase, picks by role with order tags, lane matchup favourability, the
win-probability timeline and a review of how our draft went.

Old games miss P(win) after each step: it is computed on first view (with the current model and
stats) and stored. Lane matchups are computed on first view from the game's patch and our
players' brackets, and cached on the draft.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from draftassist.data.aggregate import PAIR_K, shrink
from draftassist.data.matchup_model import load_lane_slope
from draftassist.data.pools import list_players
from draftassist.data.provider import ChampionCatalog
from draftassist.data.settings_store import get_engine_settings
from draftassist.db.models import Draft, DraftAction, Matchup
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import get_format
from draftassist.engine.model import ROLE_LABELS, ROLES, sigmoid
from draftassist.engine.state import SKIPPED, DraftState

QUEUES = {420: "Solo/Duo", 440: "Flex", 700: "Clash", 0: "Custom"}
MIN_MATCHUP_GAMES = 50  # fewer games: "no data"
EVEN_PP = 1.0  # |delta| within this is shown as even


def queue_label(draft: Draft) -> str:
    if draft.queue_id in QUEUES:
        return QUEUES[draft.queue_id]
    if draft.draft_format == "tournament":
        return "Ranked 5s" if draft.queue_id else "Tournament draft"
    return f"Queue {draft.queue_id}" if draft.queue_id else "Standard draft"


def art(catalog: ChampionCatalog, champ_id: int, version: str | None) -> dict | None:
    """Champion card with art pinned to the game's patch (served and cached by /api/art)."""
    if not champ_id or champ_id == SKIPPED:
        return None
    key, name = catalog.champs.get(champ_id, (None, f"#{champ_id}"))
    v = version or catalog.patch
    return {"id": champ_id, "name": name, "icon": f"/api/art/{v}/square/{key}" if key and v else None}


def _state(draft: Draft, actions: list[DraftAction], upto: int | None = None) -> DraftState:
    champs = tuple(a.champ_id for a in actions[: upto if upto is not None else len(actions)])
    return DraftState(get_format(draft.draft_format or "tournament"), draft.side, champs)


def _ensure_win_probs(s: Session, engine: DraftEngine, draft: Draft, actions: list[DraftAction]) -> bool:
    """Backfill P(win) after each step for games logged before it was stored. True if it did."""
    if all(a.win_prob_after is not None for a in actions):
        return False
    for i, a in enumerate(actions):
        a.win_prob_after = engine.win_prob(_state(draft, actions, i + 1))
        s.add(a)
    s.commit()
    return True


def _actions(s: Session, draft_ids: list[int]) -> dict[int, list[DraftAction]]:
    out: dict[int, list[DraftAction]] = {i: [] for i in draft_ids}
    for a in s.exec(select(DraftAction).where(col(DraftAction.draft_id).in_(draft_ids)).order_by(DraftAction.seq)):
        out[a.draft_id].append(a)
    return out


def _roles(engine: DraftEngine, draft: Draft, actions: list[DraftAction]) -> tuple[dict[int, str], dict[int, str]]:
    """Our picks' roles (from the client when logged, else the model's assignment) and theirs."""
    ev = engine.evaluate(_state(draft, actions))
    ours = dict(ev.our_roles)
    for a in actions:
        if a.role and a.side == draft.side and a.type == "pick":
            ours[a.champ_id] = a.role
    return ours, ev.their_roles


# ---------- list ----------
def plan_adherence(draft: Draft, actions: list[DraftAction]) -> dict | None:
    """Was the plan pinned for this game followed? yes = every plan champion picked by us, partly = some,
    no = none (docs/synergy-v2.md, Stage 6). None when no plan was pinned."""
    if not draft.plan_json:
        return None
    plan = json.loads(draft.plan_json)
    champs = [int(c) for c in plan.get("locks", {}).values()]
    ours = {a.champ_id for a in actions if a.type == "pick" and a.side == draft.side}
    picked = sum(c in ours for c in champs)
    followed = "yes" if champs and picked == len(champs) else "partly" if picked else "no"
    return {"followed": followed, "picked": picked, "total": len(champs)}


def history_rows(db: Engine, catalog: ChampionCatalog, queue: str | None = None, side: str | None = None,
                 result: str | None = None, patch: str | None = None, limit: int = 200) -> list[dict]:
    with Session(db) as s:
        drafts = s.exec(select(Draft).order_by(col(Draft.played_at).desc(), col(Draft.draft_id).desc()).limit(limit)).all()
        acts = _actions(s, [d.draft_id for d in drafts])
    rows = []
    for d in drafts:
        label = queue_label(d)
        d_patch = d.patch or (".".join(catalog.patch.split(".")[:2]) if catalog.patch else None)
        if (queue and label != queue) or (side and d.side != side) or (result and d.result != result) \
                or (patch and d_patch != patch):
            continue
        picks = [a for a in acts[d.draft_id] if a.type == "pick"]
        ours = [a for a in acts[d.draft_id] if a.side == d.side]
        followed = [a for a in ours if a.suggestions_json and json.loads(a.suggestions_json)["kind"] == "suggested"]
        rows.append({
            "draft_id": d.draft_id, "game_id": d.game_id, "result": d.result, "side": d.side,
            "queue": label, "format": d.draft_format, "patch": d_patch, "played_at": d.played_at.isoformat(),
            "pred_win_prob": d.pred_win_prob, "game_length_s": d.game_length_s,
            "our_champs": [art(catalog, a.champ_id, d.ddragon_version) for a in picks if a.side == d.side],
            "their_champs": [art(catalog, a.champ_id, d.ddragon_version) for a in picks if a.side != d.side],
            "followed_top": sum(1 for a in followed if a.our_rank == 1), "our_turns": len(followed),
            "match_verified": d.match_verified, "result_source": d.result_source,
            "plan": plan_adherence(d, acts[d.draft_id]),
        })
    return rows


def history_filters(rows: list[dict]) -> dict:
    return {"queue": sorted({r["queue"] for r in rows}), "patch": sorted({r["patch"] for r in rows if r["patch"]}),
            "side": ["blue", "red"], "result": ["win", "loss", "remake"]}


# ---------- lane matchups ----------
LANE_RECAP_K = 50  # a past game's lane figure: shrunk towards even, like its game figure
LANE_RECAP_VERSION = 2


def lane_matchup(db: Engine, patch: str | None, bracket: str, role: str, ours: int, theirs: int) -> dict:
    """Favourability of our champion vs theirs in pp (shrunk towards 50%), or no data."""
    if not patch:
        return {"delta_pp": None, "games": 0, "band": "no-data"}
    a, b = min(ours, theirs), max(ours, theirs)
    with Session(db) as s:
        m = s.get(Matchup, (patch, bracket, role, a, b))
    if m is None or m.games < MIN_MATCHUP_GAMES:
        return {"delta_pp": None, "games": m.games if m else 0, "band": "no-data"}
    wins_ours = m.wins_a if ours == a else m.games - m.wins_a
    game = 100 * (shrink(wins_ours, m.games, PAIR_K) - 0.5)
    delta, gold, xp = game, None, None
    if m.lane_n:  # the lane and game matchups, weighted as in Settings (docs/matchup-archetypes.md, Stage 4)
        sign = 1 if ours == a else -1
        shrunk = sign * m.lane_gold_sum / (m.lane_n + LANE_RECAP_K)
        lane = 100 * (sigmoid(shrunk / 1000 * load_lane_slope(db, role)) - 0.5)
        weight = get_engine_settings(db)["lane_weight"]
        delta = weight * lane + (1 - weight) * game
        gold, xp = round(sign * m.lane_gold_sum / m.lane_n), round(sign * m.lane_xp_sum / m.lane_n)
    band = "even" if abs(delta) <= EVEN_PP else ("ours" if delta > 0 else "theirs")
    return {"delta_pp": round(delta, 1), "game_pp": round(game, 1), "lane_gold": gold, "lane_xp": xp,
            "games": m.games, "band": band}


# ---------- recap ----------
def _load(db: Engine, catalog: ChampionCatalog, engine: DraftEngine,
          draft_id: int) -> tuple[Draft, list[DraftAction], bool] | None:
    """The draft and its actions, P(win) per step backfilled and the patch filled in if missing.
    The bool says whether P(win) had to be backfilled."""
    with Session(db) as s:
        draft = s.get(Draft, draft_id)
        if draft is None:
            return None
        backfilled = _ensure_win_probs(s, engine, draft, _actions(s, [draft_id])[draft_id])
        if not draft.patch and catalog.patch:
            draft.patch, draft.ddragon_version = ".".join(catalog.patch.split(".")[:2]), catalog.patch
            s.add(draft)
            s.commit()
        s.refresh(draft)
        return draft, _actions(s, [draft_id])[draft_id], backfilled


def _bans_by_phase(actions: list[DraftAction], steps, card) -> list[dict]:
    """Bans by phase, in the order they were banned; skipped = None ("—")."""
    phases: dict[int, dict[str, list]] = {}
    for a, step in zip(actions, steps):
        if step.kind == "ban":
            phases.setdefault(step.phase, {"blue": [], "red": []})[step.side].append(card(a.champ_id))
    return [{"phase": ph, "label": f"Phase {ph}" if len(phases) > 1 else "Bans", **sides}
            for ph, sides in sorted(phases.items())]


def _pick_tags(actions: list[DraftAction], steps) -> dict[tuple[str, int], str]:
    """(side, champ) -> B1..B5 / R1..R5 in pick order."""
    tags, counts = {}, {"blue": 0, "red": 0}
    for a, step in zip(actions, steps):
        if step.kind == "pick":
            counts[step.side] += 1
            tags[(step.side, a.champ_id)] = f"{step.side[0].upper()}{counts[step.side]}"
    return tags


def _lane_matchups(db: Engine, draft: Draft, by_role: dict[str, dict[str, int]], backfilled: bool) -> dict:
    """The recap cache: lane matchups computed on first view (again after a P(win) backfill)."""
    our_side = draft.side
    their_side = "red" if our_side == "blue" else "blue"
    cache = json.loads(draft.recap_cache) if draft.recap_cache else {}
    if backfilled:
        cache["backfilled"] = True
        cache.pop("lane_matchups", None)
    if cache.get("lane_version") != LANE_RECAP_VERSION:  # worked out before lane and game were separate
        cache.pop("lane_matchups", None)
        cache["lane_version"] = LANE_RECAP_VERSION
    if "lane_matchups" not in cache:
        brackets = {p["role"]: p["bracket"] for p in list_players(db)}
        cache["lane_matchups"] = {
            r: lane_matchup(db, draft.patch, brackets.get(r, "gold"), r, sides[our_side], sides[their_side])
            for r, sides in by_role.items() if our_side in sides and their_side in sides}
        with Session(db) as s:
            d = s.get(Draft, draft.draft_id)
            d.recap_cache = json.dumps(cache)
            s.add(d)
            s.commit()
    return cache


def _timeline(actions: list[DraftAction], steps, tags, card) -> tuple[list[dict], dict | None]:
    """P(win) after every step, and the step that moved it most."""
    timeline, prev, swing = [], 0.5, None
    for a, step in zip(actions, steps):
        wp = a.win_prob_after
        label = tags.get((step.side, a.champ_id)) if step.kind == "pick" else "ban"
        point = {"step": a.seq, "label": label, "side": step.side, "type": step.kind, "champ": card(a.champ_id),
                 "win_prob": round(wp, 4), "delta": round(wp - prev, 4)}
        timeline.append(point)
        if swing is None or abs(point["delta"]) > abs(swing["delta"]):
            swing = point
        prev = wp
    return timeline, swing


def _followed(actions: list[DraftAction], steps, our_side: str, card) -> list[dict]:
    """Our steps: what we did, where it ranked among what the screen suggested."""
    out = []
    for a, step in zip(actions, steps):
        if step.side != our_side:
            continue
        shown = json.loads(a.suggestions_json) if a.suggestions_json else None
        rank = (str(a.our_rank) if a.our_rank else "off-list") if shown and shown["kind"] == "suggested" else "—"
        out.append({"step": a.seq, "type": step.kind, "champ": card(a.champ_id), "rank": rank,
                    "suggested": [card(i["champ_id"]) for i in shown["items"]] if shown else []})
    return out


def _plan_detail(draft: Draft, actions: list[DraftAction], card) -> dict | None:
    plan = plan_adherence(draft, actions)
    if plan is not None:
        locks = json.loads(draft.plan_json).get("locks", {})
        ours = {a.champ_id for a in actions if a.type == "pick" and a.side == draft.side}
        plan["champs"] = [{**card(int(c)), "role": r, "picked": int(c) in ours} for r, c in locks.items()]
    return plan


def _win_pp(x: float) -> float:
    """A log-odds term as pp of win chance around 50%."""
    return round(100 * (sigmoid(x) - 0.5), 1)


def recap(db: Engine, engine: DraftEngine, catalog: ChampionCatalog, draft_id: int) -> dict | None:
    loaded = _load(db, catalog, engine, draft_id)
    if loaded is None:
        return None
    draft, actions, backfilled = loaded
    fmt = get_format(draft.draft_format or "tournament")
    steps = fmt.steps
    version = draft.ddragon_version

    def card(c: int) -> dict | None:
        return art(catalog, c, version)

    our_side = draft.side
    their_side = "red" if our_side == "blue" else "blue"
    players = json.loads(draft.players_json) if draft.players_json else {}
    if not players:
        players = {p["role"]: p["riot_id"].split("#")[0] for p in list_players(db) if p["riot_id"]}

    our_roles, their_roles = _roles(engine, draft, actions)
    tags = _pick_tags(actions, steps)
    by_role: dict[str, dict[str, int]] = {r: {} for r in ROLES}
    for c, r in our_roles.items():
        by_role[r][our_side] = c
    for c, r in their_roles.items():
        by_role[r][their_side] = c
    cache = _lane_matchups(db, draft, by_role, backfilled)
    picks = []
    for r in ROLES:
        row = {"role": r, "role_label": ROLE_LABELS[r], "matchup": cache["lane_matchups"].get(r)}
        for side in ("blue", "red"):
            c = by_role[r].get(side)
            row[side] = None if c is None else {
                "champ": card(c), "tag": tags.get((side, c)), "player": players.get(r) if side == our_side else None}
        picks.append(row)

    timeline, swing = _timeline(actions, steps, tags, card)
    terms = engine.evaluate(_state(draft, actions)).terms
    full = [{"step": a.seq, "side": step.side, "ours": step.side == our_side, "type": step.kind,
             "champ": card(a.champ_id), "rank": a.our_rank, "win_prob": round(a.win_prob_after, 4)}
            for a, step in zip(actions, steps)]
    played = draft.played_at if isinstance(draft.played_at, datetime) else datetime.fromisoformat(str(draft.played_at))
    return {
        "plan": _plan_detail(draft, actions, card),
        "draft_id": draft.draft_id,
        "header": {"result": draft.result, "queue": queue_label(draft), "patch": draft.patch,
                   "played_at": played.isoformat(), "game_length_s": draft.game_length_s, "side": our_side,
                   "pred_win_prob": draft.pred_win_prob, "match_verified": draft.match_verified,
                   "format": fmt.name},
        "bans": _bans_by_phase(actions, steps, card),
        "picks": picks,
        "timeline": timeline,
        "review": {"followed": _followed(actions, steps, our_side, card), "biggest_swing": swing,
                   "synergy": {"ours_pp": _win_pp(terms.syn_us), "theirs_pp": _win_pp(terms.syn_them),
                               "net_pp": round(_win_pp(terms.syn_us) - _win_pp(terms.syn_them), 1)}},
        "table": full,
        "note": ("Logged before per-step P(win) was stored: the timeline was computed later, with the model "
                 "and stats of that day") if cache.get("backfilled") else None,
    }
