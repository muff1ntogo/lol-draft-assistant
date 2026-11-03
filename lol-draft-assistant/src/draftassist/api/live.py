"""Live screen: champ select session -> view payload -> WebSocket push to every open screen.

Every session event is parsed and scored from scratch; nothing is cached across events, so a
champion locked a moment ago is gone from the next push.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import WebSocket, WebSocketDisconnect

from draftassist.api.draftlog import DraftLogger
from draftassist.data.provider import ChampionCatalog
from draftassist.engine.comp import (
    NARROW_CARDS,
    STYLE_LABELS,
    ChampionProfiles,
    badge,
    card_count,
    comp_effect,
    team_comp,
)
from draftassist.engine.engine import DraftEngine, Evaluation, Suggestion
from draftassist.engine.guards import strict_mode
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.state import DraftState
from draftassist.lcu.parser import LiveDraft, SessionParseError, parse_session
from draftassist.lcu.recorder import SessionRecorder

log = logging.getLogger(__name__)

class PlayerDirectory(Protocol):
    """Our players: PUUID -> role for the parser, and filling PUUIDs from champ select teammates."""

    def roles_by_puuid(self) -> dict[str, str]: ...

    def resolve_from_session(self, members: list[dict]) -> int: ...


HOVER_FLAG_RISK = 0.2  # log-odds; a hover whose best open counter wins its lane ~55%+ gets flagged
IDLE_PHASES = {"None", "Lobby", "Matchmaking", "ReadyCheck"}  # gameflow phases that clear the screen


class LiveHub:
    """Holds the latest payload and pushes each new one to every connected screen."""

    def __init__(self) -> None:
        self.latest: dict = {"status": "waiting", "message": "Starting up"}
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def publish(self, payload: dict) -> None:
        self.latest = payload
        for ws in list(self._clients):
            try:
                await ws.send_json(payload)
            except Exception:
                self._clients.discard(ws)

    def publish_threadsafe(self, payload: dict) -> None:
        if self._loop is None or self._loop.is_closed():
            self.latest = payload
            return
        asyncio.run_coroutine_threadsafe(self.publish(payload), self._loop)

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        try:
            await ws.send_json(self.latest)
            while True:
                await ws.receive_text()  # screens don't send anything; this detects disconnects
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(ws)


def _ban_card(catalog: ChampionCatalog, s: Suggestion, hovered: set[int], weights=None,
              profiles: ChampionProfiles | None = None) -> dict:
    card = {"champ": catalog.card(s.champ_id), "reason": s.reason, "value": round(s.value, 4),
            "relevance": None if s.relevance is None else round(s.relevance, 3),
            "discount": None if s.discount is None else round(s.discount, 3),
            "warnings": list(s.warnings), "highlighted": s.champ_id in hovered, "plan": s.plan}
    if s.threat is not None and weights is not None:
        # Each component in log-odds, unweighted and as it enters the ban value (weighted).
        card["components"] = {
            name: {"raw": round(raw, 5), "weighted": round(weight * raw, 5)}
            for name, raw, weight in (("threat", s.threat, weights.threat), ("matchup", s.matchup, weights.matchup),
                                      ("comp", s.comp, weights.comp), ("own", s.own_loss or 0.0, weights.own))}
    if profiles is not None:
        card["badge"] = badge(profiles, s.champ_id, s.role).as_dict()
    return card


NO_USEFUL_BAN = "no useful ban \u2014 free choice"


def _ban_group(engine: DraftEngine, catalog: ChampionCatalog, live: LiveDraft, hovered: set[int],
               bonus: dict[int, float] | None = None) -> dict:
    """Simultaneous bans: one card per teammate with our coordinated suggestion and their status."""
    st = live.state
    open_bans = [b for b in live.ally_bans if b.status in ("pending", "hovering")]
    teammates_hovering = {b.champ_id for b in open_bans if b.status == "hovering"}
    chosen = engine.ban_group(st.with_provisional(teammates_hovering), [b.role for b in open_bans], bonus=bonus)
    suggestion_for = {id(b): s for b, (_, s) in zip(open_bans, chosen)}
    order = {r: i for i, r in enumerate(ROLES)}
    cards = []
    for b in sorted(live.ally_bans, key=lambda b: (order.get(b.role or "", 9), b.cell_id)):
        s = suggestion_for.get(id(b))
        cards.append({
            "role": b.role, "role_label": ROLE_LABELS.get(b.role or "", "Teammate"), "player": b.player,
            "status": "suggested" if b.status == "pending" else b.status,
            "status_champ": catalog.card(b.champ_id) if b.status in ("hovering", "locked") else None,
            "suggestion": _ban_card(catalog, s, hovered, engine.cfg.ban_weights, engine.data.profiles) if s else None,
            "note": None if s or b.status in ("locked", "skipped") else NO_USEFUL_BAN,
        })
    return {"mode": "our_ban_group", "title": "BANS", "cards": cards}


def _pick_card(catalog: ChampionCatalog, s: Suggestion, hovered: set[int], profiles: ChampionProfiles,
               ours: list[tuple[int, str | None]]) -> dict:
    """ours: our locked picks with roles, for what this pick does to our comp."""
    warnings = []
    if s.champ_id in hovered and s.counter_risk >= HOVER_FLAG_RISK and s.counter_id:
        warnings.append(f"⚠ easy to counter: {catalog.card(s.counter_id)['name']}")
    return {"champ": catalog.card(s.champ_id), "reason": s.reason, "role": s.role,
            "role_label": ROLE_LABELS.get(s.role or "", ""), "win_prob": round(s.win_prob, 4),
            "warnings": warnings, "highlighted": s.champ_id in hovered, "plan": s.plan,
            "badge": badge(profiles, s.champ_id, s.role).as_dict(),
            "effect": comp_effect(profiles, ours, s.champ_id, s.role)}


def _target(engine: DraftEngine, ours: list[tuple[int, str | None]]) -> dict | None:
    """The style our picks are pulled towards (comp coherence), for the comp panel."""
    style, source = engine.target_for(ours)
    return None if style is None else {"style": style, "label": STYLE_LABELS[style], "source": source}


def _score_hover(engine: DraftEngine, state: DraftState, open_roles: list[str], champ: int,
                 role: str | None) -> Suggestion | None:
    """Our engine's view of a teammate's hover, wherever it ranks."""
    ranked = engine.suggest_picks(state, k=10_000, role=role if role in open_roles else None)
    return next((s for s in ranked if s.champ_id == champ), None)


def _comp_summary(engine: DraftEngine, comp: list[tuple[int, str]]) -> dict:
    """Damage split, comp timing (Early / Mix / Late) and fight style for a set of champions."""
    known = [d for c, r in comp if (d := engine.data.damage.get((c, r))) is not None]
    damage = None
    if known:
        tot = [sum(d[i] for d in known) for i in range(3)]
        z = sum(tot) or 1.0
        damage = {"physical": round(tot[0] / z, 4), "magic": round(tot[1] / z, 4), "true": round(tot[2] / z, 4)}
    return {**team_comp(engine.data.profiles, list(comp)).as_dict(), "damage": damage}


def _plan_slot_status(st: DraftState, champ: int, role: str, by_role: dict[str, int],
                      at_risk: list[int]) -> tuple[str, int | None]:
    """A planned champion against the draft: (status, the champion we picked in its role instead, if any)."""
    if champ in st.our_picks:
        return "picked", None
    if role in by_role:
        return "replaced", by_role[role]
    if champ in st.bans():
        return "banned", None
    if champ in st.their_picks:
        return "taken", None
    return ("at_risk" if champ in at_risk else "planned"), None


def _plan_ban_status(st: DraftState, champ: int) -> str:
    if champ in st.bans(st.our_side):
        return "ours"
    if champ in st.bans(st.their_side):
        return "theirs"
    if champ in st.our_picks or champ in st.their_picks:
        return "picked"
    return "open"


def _plan_bar(engine: DraftEngine, catalog: ChampionCatalog, pin: dict, banner: dict | None = None,
              st: DraftState | None = None, our_roles: dict[int, str] | None = None) -> dict:
    """The pinned plan's bar at the bottom of the Live screen (docs/synergy-v2.md, Stage 6): each planned
    champion with its status against the live draft, the plan's bans, our off-plan bans, and the comp
    summaries of the plan and of where the draft stands now (our picks + the plan's remaining champions,
    or their backups once gone). Without a draft (waiting) only the plan itself."""
    card = catalog.card
    locks = {r: int(c) for r, c in pin["locks"].items()}
    backups = {r: (int(b) if b else None) for r, b in (pin.get("backups") or {}).items()}
    plan_comp = [(locks[r], r) for r in ROLES if r in locks]
    banner = banner or {"state": "intact", "champs": locks, "at_risk": [], "gone": [], "picked": []}
    label = {"intact": "intact", "at_risk": "at risk", "broken": "broken"}[banner["state"]]
    out = {"state": banner["state"], "label": label,
           "text": f"Plan: {' · '.join(card(c)['name'] for c, _ in plan_comp)} — {label}",
           "champs": [{**card(c), "role": r} for c, r in plan_comp],
           "at_risk": [card(c) for c in banner["at_risk"]], "picked": banner["picked"],
           "gone": [{"champ": card(g["champ"]), "role": g["role"], "role_label": ROLE_LABELS[g["role"]],
                     "backup": card(g["backup"]) if g["backup"] else None} for g in banner["gone"]]}

    def slot(c: int, r: str, status: str, actual: int | None) -> dict:
        return {"role": r, "role_label": ROLE_LABELS[r], "planned": card(c), "status": status,
                "actual": card(actual) if actual else None,
                "backup": card(backups[r]) if backups.get(r) else None,
                "badge": badge(engine.data.profiles, c, r).as_dict()}

    if st is None:
        return out | {"slots": [slot(c, r, "planned", None) for c, r in plan_comp], "bans": None,
                      "summary": {"plan": _comp_summary(engine, plan_comp), "now": None}}

    our_roles = our_roles or {}
    by_role = {r: c for c, r in our_roles.items()}
    unavailable = st.unavailable()
    slots, now_comp = [], list(our_roles.items())
    for c, r in plan_comp:
        status, actual = _plan_slot_status(st, c, r, by_role, banner["at_risk"])
        slots.append(slot(c, r, status, actual))
        if r not in by_role and status != "picked":  # still to come: the plan champion, or its backup once gone
            fill = c if c not in unavailable else backups.get(r)
            if fill and fill not in unavailable:
                now_comp.append((fill, r))
    plan_bans = [int(c) for c in pin.get("bans", {}).get(st.format.name, {}).get(st.our_side, [])]
    return out | {
        "slots": slots,
        "bans": {"plan": [{"champ": card(c), "status": _plan_ban_status(st, c)} for c in plan_bans],
                 "off_plan": [card(c) for c in st.bans(st.our_side) if c not in plan_bans]},
        "summary": {"plan": _comp_summary(engine, plan_comp), "now": _comp_summary(engine, now_comp)}}


@dataclass
class _View:
    """What every part of one Live screen payload is built from."""
    engine: DraftEngine
    catalog: ChampionCatalog
    live: LiveDraft
    ev: Evaluation
    ban_bonus: dict[int, float] | None
    pick_bonus: dict[int, float] | None

    @property
    def st(self) -> DraftState:
        return self.live.state

    @property
    def profiles(self) -> ChampionProfiles:
        return self.engine.data.profiles

    @property
    def hovered(self) -> set[int]:
        return {c for c, _ in self.st.hovers}

    @property
    def ours(self) -> list[tuple[int, str | None]]:
        return [(c, self.ev.our_roles.get(c)) for c in self.st.our_picks]

    @property
    def open_roles(self) -> list[str]:
        return [r for r in ROLES if r not in self.ev.our_roles.values()]

    def card(self, champ: int | None) -> dict | None:
        return self.catalog.card(champ)

    def badge_of(self, champ: int, role: str | None) -> dict | None:
        return badge(self.profiles, champ, role).as_dict() if champ else None


def _phase_label(live: LiveDraft, finished: bool) -> str:
    if live.timer_phase == "PLANNING":
        return "Planning"
    return "Locked in" if finished else live.state.step.label


def _our_slots(v: _View, finished: bool) -> list[dict]:
    """Our five slots in role order, with a score for a teammate's hover while the draft is open."""
    st, ev, card = v.st, v.ev, v.card
    role_order = {r: i for i, r in enumerate(ROLES)}
    out = []
    for s in sorted(v.live.our_slots, key=lambda s: (role_order.get(s.role or ev.our_roles.get(s.champ_id, ""), 9),
                                                      s.cell_id)):
        role = s.role or ev.our_roles.get(s.champ_id)
        slot = {"role": role, "role_label": ROLE_LABELS.get(role or "", "?"), "champ": card(s.champ_id),
                "hover": card(s.hover_id), "is_local": s.is_local, "hover_score": None,
                "badge": v.badge_of(s.champ_id, role)}
        if s.hover_id and s.hover_id not in st.unavailable() and not finished:
            scored = _score_hover(v.engine, st, v.open_roles, s.hover_id, s.role)
            if scored is not None:
                flag = (f"⚠ easy to counter: {card(scored.counter_id)['name']}"
                        if scored.counter_risk >= HOVER_FLAG_RISK and scored.counter_id else None)
                slot["hover_score"] = {"win_prob": round(scored.win_prob, 4), "reason": scored.reason, "warning": flag}
        out.append(slot)
    return out


def _their_slots(v: _View) -> list[dict]:
    roles = v.ev.their_roles
    return [{"champ": v.card(s.champ_id), "role": roles.get(s.champ_id),
             "role_label": ROLE_LABELS.get(roles.get(s.champ_id, ""), ""),
             "badge": v.badge_of(s.champ_id, roles.get(s.champ_id))}
            for s in v.live.their_slots]


def _center_locked(v: _View) -> dict:
    return {"mode": "locked", "win_prob": round(v.ev.win_prob, 4),
            "threats": [{"champ": v.card(t.champ_id), "reason": t.reason} for t in v.engine.threats(v.st, k=2)]}


def _center_our_ban(v: _View, n_cards: int) -> dict:
    """Ranked bans are simultaneous: show ours while any teammate is still banning, minus the champions
    teammates are already about to ban."""
    st, about_to_ban = v.st, v.live.ally_ban_hovers
    n = NARROW_CARDS if st.format.simultaneous_bans else n_cards  # Flex bans: unchanged
    bans = [s for s in v.engine.suggest_bans(st, k=n + len(about_to_ban), bonus=v.ban_bonus)
            if s.champ_id not in about_to_ban][:n]
    return {"mode": "our_ban", "title": "BAN",
            "cards": [_ban_card(v.catalog, s, v.hovered, v.engine.cfg.ban_weights, v.profiles) for s in bans]}


def _next_two(v: _View) -> list[dict]:
    """Two picks in a row: the best pairs, each pick with its meta and our player's record."""
    eng, card = v.engine, v.card
    return [
        {"picks": [{"champ": card(s.champ_id), "role": s.role, "role_label": ROLE_LABELS[s.role], "reason": s.reason,
                    "badge": v.badge_of(s.champ_id, s.role),
                    **{k: round(x, 4) if isinstance(x, float) else x
                       for k, x in eng.pick_record(s.champ_id, s.role).items()}}
                   for s in (pair.first, pair.second)],
         "win_prob": round(pair.win_prob, 4), "synergy_pp": round(pair.synergy_pp, 1), "low_data": pair.low_data}
        for pair in eng.suggest_pick_pairs(v.st, k=3)]


def _center_our_pick(v: _View, n_cards: int) -> dict:
    st, live, eng = v.st, v.live, v.engine
    role = live.actor_role if live.actor_role in v.open_roles else None
    picks = eng.suggest_picks(st, k=n_cards, role=role, bonus=v.pick_bonus)
    center = {"mode": "our_pick", "title": f"PICK — {ROLE_LABELS[role]}" if role else "PICK",
              "cards": [_pick_card(v.catalog, s, v.hovered, v.profiles, v.ours) for s in picks], "hover_card": None}
    if eng.back_to_back(st):
        center["next_two"] = _next_two(v)
    actor_hover = next((sl.hover_id for sl in live.our_slots if sl.cell_id == live.actor_cell_id), 0)
    if actor_hover and actor_hover not in {s.champ_id for s in picks}:
        scored = _score_hover(eng, st, v.open_roles, actor_hover, live.actor_role)
        if scored is not None:
            center["hover_card"] = _pick_card(v.catalog, scored, v.hovered, v.profiles, v.ours)
    return center


def _center_enemy_turn(v: _View) -> dict:
    """Their likely picks, each with our pool's best lane answer."""
    st, card = v.st, v.card
    tables = v.engine.lane_ban_tables(st)
    cards = []
    for s in v.engine.likely_enemy_picks(st, k=3):
        row = tables[s.role].row(s.champ_id) if s.role else None
        answer = None
        if row is not None and row.via is not None:
            answer = {"champ": card(row.via), "resp_pp": round(row.resp, 1)}
        cards.append({"champ": card(s.champ_id), "prob": round(s.enemy_prob, 4), "role": s.role,
                      "role_label": ROLE_LABELS.get(s.role or "", ""), "answer": answer,
                      "badge": v.badge_of(s.champ_id, s.role),
                      "answer_note": None if answer else (row.via_note if row else "no answer in our pool")})
    return {"mode": "enemy_turn", "title": f"THEIR {'BAN' if st.step.kind == 'ban' else 'PICK'}", "cards": cards}


def build_live_view(engine: DraftEngine, catalog: ChampionCatalog, live: LiveDraft,
                    data_source: str, note: str | None, pin: dict | None = None) -> dict:
    st = live.state
    ev = engine.evaluate(st)
    effects = None
    if pin:
        from draftassist.engine.plan import pin_effects

        effects = pin_effects(pin, st, live.enemy_hovers, frozenset(ev.our_roles.values()))
    v = _View(engine, catalog, live, ev, effects.ban_bonus if effects else None,
              effects.pick_bonus if effects else None)
    step = st.step
    finished = step is None or live.timer_phase in ("FINALIZATION", "GAME_STARTING")
    ours = v.ours
    theirs = [(c, ev.their_roles.get(c)) for c in st.their_picks]
    n_cards = card_count(step.kind if step else None, step.phase if step else None, len(ours))

    center: dict[str, Any]
    if finished:
        center = _center_locked(v)
    elif st.format.simultaneous_bans and any(b.status in ("pending", "hovering") for b in live.ally_bans):
        center = _ban_group(engine, catalog, live, v.hovered, v.ban_bonus)
    elif live.ally_action == "ban" or (st.is_our_turn and step.kind == "ban"):
        center = _center_our_ban(v, n_cards)
    elif st.is_our_turn:
        center = _center_our_pick(v, n_cards)
    else:
        center = _center_enemy_turn(v)

    card = catalog.card
    return {
        "status": "champ_select", "data_source": data_source, "note": note,
        "format": st.format.name, "side": st.our_side, "phase": _phase_label(live, finished),
        "turn": None if finished else ("us" if st.is_our_turn else "them"),
        "action": None if finished else step.kind, "seconds_left": live.seconds_left, "game_id": live.game_id,
        "our_slots": _our_slots(v, finished), "their_slots": _their_slots(v),
        "our_bans": [card(c) for c in st.bans(st.our_side)],
        "their_bans": [card(c) for c in st.bans(st.their_side)],
        "comp": {"ours": {**team_comp(v.profiles, ours).as_dict(), "target": _target(engine, ours)},
                 "theirs": team_comp(v.profiles, theirs).as_dict()},
        "center": center,
        "plan": _plan_bar(engine, catalog, pin, effects.banner, st, ev.our_roles) if effects else None,
    }


class LiveService:
    """Turns client events into Live screen payloads and draft logs. Safe to call from the
    connector thread."""

    def __init__(self, engine: DraftEngine, catalog: ChampionCatalog, hub: LiveHub, data_source: str,
                 note: str | None, draft_format: str = "auto", recorder: SessionRecorder | None = None,
                 logger: DraftLogger | None = None, players: PlayerDirectory | None = None,
                 on_game_logged: Callable[[int], None] | None = None):
        self.engine, self.catalog, self.hub = engine, catalog, hub
        self.data_source, self.note, self.draft_format, self.recorder = data_source, note, draft_format, recorder
        self.logger, self.players, self.on_game_logged = logger, players, on_game_logged
        self.client_status = "starting"
        self.gameflow_phase: str | None = None
        self.queue_id: int | None = None
        self._resolved_for: int | None = None  # game id whose teammates were already matched to players
        self.current: LiveDraft | None = None  # latest parsed champ select (Prep page: "Use current draft")
        self.pin_provider: Callable[[], dict | None] | None = None  # the pinned plan (docs/synergy-v2.md, Stage 6)

    def _waiting(self) -> dict:
        pin = self.pin_provider() if self.pin_provider else None
        return {"status": "waiting", "message": "Waiting for champ select", "client": self.client_status,
                "gameflow_phase": self.gameflow_phase, "data_source": self.data_source, "note": self.note,
                "plan": _plan_bar(self.engine, self.catalog, pin) if pin else None}

    def _build(self, session: dict | None) -> tuple[dict, LiveDraft | None]:
        if session is None:
            self.current = None
            return self._waiting(), None
        try:
            roles = self.players.roles_by_puuid() if self.players else None
            live = parse_session(session, self.draft_format, roles)
            self.current = live
            pin = self.pin_provider() if self.pin_provider else None
            return build_live_view(self.engine, self.catalog, live, self.data_source, self.note, pin), live
        except SessionParseError as e:
            log.error("could not parse champ select session: %s", e)
            return {"status": "error", "message": str(e)}, None
        except Exception:
            if strict_mode():
                raise
            log.exception("live view failed")
            return {"status": "error", "message": "Suggestion error (see server log)"}, None

    def view(self, session: dict | None) -> dict:
        return self._build(session)[0]

    def handle_session(self, session: dict | None) -> dict:
        if self.recorder is not None:
            if session is None:
                self.recorder.end()
            else:
                self.recorder.record("session", session)
        latest = self.hub.latest
        if session is None and latest.get("status") == "champ_select" and latest.get("phase") == "Locked in":
            return latest  # game is loading: keep win % and threats up until the client goes idle
        payload, live = self._build(session)
        self.hub.publish_threadsafe(payload)
        if live is not None:
            if self.players is not None and self._resolved_for != (live.game_id or -1):
                self._resolved_for = live.game_id or -1
                self.players.resolve_from_session(session.get("myTeam") or [])
            if self.logger is not None:
                self.logger.observe(live, payload)
        return payload

    def handle_phase(self, phase: str, gameflow_session: dict | None) -> None:
        self.gameflow_phase = phase
        if gameflow_session:
            self.queue_id = (gameflow_session.get("gameData") or {}).get("queue", {}).get("id")
            if self.recorder is not None and phase == "ChampSelect":
                self.recorder.record("gameflow", gameflow_session)
        if self.logger is not None:
            self.logger.set_queue(self.queue_id)
            self.logger.on_phase(phase)
        if phase in IDLE_PHASES:
            self.hub.publish_threadsafe(self._waiting())

    def handle_end_of_game(self, eog: dict | None) -> int | None:
        if self.recorder is not None and eog:
            self.recorder.record("eog", eog)
        if self.logger is None:
            return None
        draft_id = self.logger.on_end_of_game(eog)
        if draft_id is not None and self.on_game_logged is not None:
            self.on_game_logged(draft_id)
        return draft_id

    def handle_status(self, status: str) -> None:
        self.client_status = status
        if self.hub.latest.get("status") != "champ_select":
            self.hub.publish_threadsafe(self._waiting())
