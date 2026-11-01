"""Live screen: champ select session -> view payload -> WebSocket push to every open screen.

Every session event is parsed and scored from scratch; nothing is cached across events, so a
champion locked a moment ago is gone from the next push.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from draftassist.data.provider import ChampionCatalog
from draftassist.engine.engine import DraftEngine, Suggestion
from draftassist.engine.guards import strict_mode
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.state import DraftState
from draftassist.lcu.parser import LiveDraft, SessionParseError, parse_session
from draftassist.lcu.recorder import SessionRecorder

log = logging.getLogger(__name__)

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


def _ban_card(catalog: ChampionCatalog, s: Suggestion, hovered: set[int]) -> dict:
    return {"champ": catalog.card(s.champ_id), "reason": s.reason, "value": round(s.value, 4),
            "relevance": None if s.relevance is None else round(s.relevance, 3),
            "warnings": list(s.warnings), "highlighted": s.champ_id in hovered}


def _pick_card(catalog: ChampionCatalog, s: Suggestion, hovered: set[int]) -> dict:
    warnings = []
    if s.champ_id in hovered and s.counter_risk >= HOVER_FLAG_RISK and s.counter_id:
        warnings.append(f"⚠ easy to counter: {catalog.card(s.counter_id)['name']}")
    return {"champ": catalog.card(s.champ_id), "reason": s.reason, "role": s.role,
            "role_label": ROLE_LABELS.get(s.role or "", ""), "win_prob": round(s.win_prob, 4),
            "warnings": warnings, "highlighted": s.champ_id in hovered}


def _score_hover(engine: DraftEngine, state: DraftState, open_roles: list[str], champ: int,
                 role: str | None) -> Suggestion | None:
    """Our engine's view of a teammate's hover, wherever it ranks."""
    ranked = engine.suggest_picks(state, k=10_000, role=role if role in open_roles else None)
    return next((s for s in ranked if s.champ_id == champ), None)


def build_live_view(engine: DraftEngine, catalog: ChampionCatalog, live: LiveDraft,
                    data_source: str, note: str | None) -> dict:
    st = live.state
    ev = engine.evaluate(st)
    card = catalog.card
    step = st.step
    finished = step is None or live.timer_phase in ("FINALIZATION", "GAME_STARTING")
    if live.timer_phase == "PLANNING":
        phase = "Planning"
    elif finished:
        phase = "Locked in"
    else:
        phase = step.label
    turn = None if finished else ("us" if st.is_our_turn else "them")
    open_roles = [r for r in ROLES if r not in ev.our_roles.values()]
    hovered = {c for c, _ in st.hovers}

    our_slots = []
    for s in live.our_slots:
        role = s.role or ev.our_roles.get(s.champ_id)
        slot = {"role": role, "role_label": ROLE_LABELS.get(role or "", "?"), "champ": card(s.champ_id),
                "hover": card(s.hover_id), "is_local": s.is_local, "hover_score": None}
        if s.hover_id and s.hover_id not in st.unavailable() and not finished:
            scored = _score_hover(engine, st, open_roles, s.hover_id, s.role)
            if scored is not None:
                flag = (f"⚠ easy to counter: {card(scored.counter_id)['name']}"
                        if scored.counter_risk >= HOVER_FLAG_RISK and scored.counter_id else None)
                slot["hover_score"] = {"win_prob": round(scored.win_prob, 4), "reason": scored.reason, "warning": flag}
        our_slots.append(slot)
    their_slots = [{"champ": card(s.champ_id), "role": ev.their_roles.get(s.champ_id),
                    "role_label": ROLE_LABELS.get(ev.their_roles.get(s.champ_id, ""), "")}
                   for s in live.their_slots]

    center: dict[str, Any]
    if finished:
        center = {"mode": "locked", "win_prob": round(ev.win_prob, 4),
                  "threats": [{"champ": card(t.champ_id), "reason": t.reason} for t in engine.threats(st, k=2)]}
    elif st.is_our_turn and step.kind == "ban":
        center = {"mode": "our_ban", "title": "BAN",
                  "cards": [_ban_card(catalog, s, hovered) for s in engine.suggest_bans(st, k=3)]}
    elif st.is_our_turn:
        role = live.actor_role if live.actor_role in open_roles else None
        picks = engine.suggest_picks(st, k=3, role=role)
        cards = [_pick_card(catalog, s, hovered) for s in picks]
        center = {"mode": "our_pick", "title": f"PICK — {ROLE_LABELS[role]}" if role else "PICK", "cards": cards,
                  "hover_card": None}
        actor_hover = next((sl.hover_id for sl in live.our_slots if sl.cell_id == live.actor_cell_id), 0)
        if actor_hover and actor_hover not in {s.champ_id for s in picks}:
            scored = _score_hover(engine, st, open_roles, actor_hover, live.actor_role)
            if scored is not None:
                center["hover_card"] = _pick_card(catalog, scored, hovered)
    else:
        tables = engine.lane_ban_tables(st)
        cards = []
        for s in engine.likely_enemy_picks(st, k=3):
            row = tables[s.role].row(s.champ_id) if s.role else None
            answer = None
            if row is not None and row.via is not None:
                answer = {"champ": card(row.via), "resp_pp": round(row.resp, 1)}
            cards.append({"champ": card(s.champ_id), "prob": round(s.enemy_prob, 4), "role": s.role,
                          "role_label": ROLE_LABELS.get(s.role or "", ""), "answer": answer,
                          "answer_note": None if answer else (row.via_note if row else "no answer in our pool")})
        center = {"mode": "enemy_turn", "title": f"THEIR {'BAN' if step.kind == 'ban' else 'PICK'}", "cards": cards}

    return {
        "status": "champ_select", "data_source": data_source, "note": note,
        "format": st.format.name, "side": st.our_side, "phase": phase, "turn": turn,
        "action": None if finished else step.kind, "seconds_left": live.seconds_left, "game_id": live.game_id,
        "our_slots": our_slots, "their_slots": their_slots,
        "our_bans": [card(c) for c in st.bans(st.our_side)],
        "their_bans": [card(c) for c in st.bans(st.their_side)],
        "center": center,
    }


class LiveService:
    """Turns client events into Live screen payloads. Safe to call from the connector thread."""

    def __init__(self, engine: DraftEngine, catalog: ChampionCatalog, hub: LiveHub, data_source: str,
                 note: str | None, draft_format: str = "auto", recorder: SessionRecorder | None = None):
        self.engine, self.catalog, self.hub = engine, catalog, hub
        self.data_source, self.note, self.draft_format, self.recorder = data_source, note, draft_format, recorder
        self.client_status = "starting"
        self.gameflow_phase: str | None = None
        self.queue_id: int | None = None

    def _waiting(self) -> dict:
        return {"status": "waiting", "message": "Waiting for champ select", "client": self.client_status,
                "gameflow_phase": self.gameflow_phase, "data_source": self.data_source, "note": self.note}

    def view(self, session: dict | None) -> dict:
        if session is None:
            return self._waiting()
        try:
            live = parse_session(session, self.draft_format)
            return build_live_view(self.engine, self.catalog, live, self.data_source, self.note)
        except SessionParseError as e:
            log.error("could not parse champ select session: %s", e)
            return {"status": "error", "message": str(e)}
        except Exception:
            if strict_mode():
                raise
            log.exception("live view failed")
            return {"status": "error", "message": "Suggestion error (see server log)"}

    def handle_session(self, session: dict | None) -> dict:
        if self.recorder is not None:
            if session is None:
                self.recorder.end()
            else:
                self.recorder.record("session", session)
        latest = self.hub.latest
        if session is None and latest.get("status") == "champ_select" and latest.get("phase") == "Locked in":
            return latest  # game is loading: keep win % and threats up until the client goes idle
        payload = self.view(session)
        self.hub.publish_threadsafe(payload)
        return payload

    def handle_phase(self, phase: str, gameflow_session: dict | None) -> None:
        self.gameflow_phase = phase
        if gameflow_session:
            self.queue_id = (gameflow_session.get("gameData") or {}).get("queue", {}).get("id")
            if self.recorder is not None and phase == "ChampSelect":
                self.recorder.record("gameflow", gameflow_session)
        if phase in IDLE_PHASES:
            self.hub.publish_threadsafe(self._waiting())

    def handle_status(self, status: str) -> None:
        self.client_status = status
        if self.hub.latest.get("status") != "champ_select":
            self.hub.publish_threadsafe(self._waiting())
