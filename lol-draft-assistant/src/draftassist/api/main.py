"""FastAPI app: Live screen (WebSocket push), Prep lane-ban tables, and the built web UI.

Run: `uv run uvicorn draftassist.api.main:app --reload`, then open http://localhost:8000
Live feed: DRAFTASSIST_LCU=client (default, the real League client) | demo (simulated draft) | off
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from draftassist.api.live import LiveHub, LiveService
from draftassist.config import PROJECT_ROOT, Settings, load_settings
from draftassist.data import ddragon
from draftassist.data.provider import ChampionCatalog, load_catalog, load_engine_inputs
from draftassist.db.session import init_db
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import TOURNAMENT, get_format
from draftassist.engine.lane_ban import RoleBanTable
from draftassist.engine.model import ROLE_LABELS, ROLES
from draftassist.engine.state import DraftState
from draftassist.lcu.demo import demo_sessions
from draftassist.lcu.recorder import SessionRecorder

log = logging.getLogger(__name__)
WEB_DIST = PROJECT_ROOT / "web" / "dist"
RECORDINGS = PROJECT_ROOT / "fixtures" / "lcu" / "recorded"


@dataclass
class Services:
    engine: DraftEngine
    catalog: ChampionCatalog
    data_source: str
    note: str | None
    hub: LiveHub
    live: LiveService
    lcu_mode: str = "off"


def build_services(settings: Settings) -> Services:
    db = init_db(settings.db_path)
    if not load_catalog(db).champs:
        try:  # first run: fetch champion names/icons so live drafts show real champions
            with httpx.Client(timeout=30) as client, Session(db) as session:
                ddragon.refresh(client, session, settings.cache_dir, settings.ddragon_locale)
        except httpx.HTTPError:
            log.warning("could not load Data Dragon; champion names will be ids")
    inputs = load_engine_inputs(db)
    engine = DraftEngine(inputs.data, inputs.team)
    hub = LiveHub()
    recorder = SessionRecorder(RECORDINGS) if settings.record_sessions and settings.lcu_mode == "client" else None
    live = LiveService(engine, inputs.catalog, hub, inputs.data_source, inputs.note, settings.draft_format, recorder)
    return Services(engine, inputs.catalog, inputs.data_source, inputs.note, hub, live, settings.lcu_mode)


async def run_demo(live: LiveService, interval: float = 1.5) -> None:
    """Replay simulated drafts as LCU session events, forever."""
    for game, side in enumerate(itertools.cycle(["blue", "red"]), start=1):
        live.handle_status("demo mode (simulated draft)")
        for session in demo_sessions(live.engine, get_format("tournament"), side, game_id=game):
            await asyncio.to_thread(live.handle_session, session)
            await asyncio.sleep(interval)
        await asyncio.sleep(interval * 3)
        await asyncio.to_thread(live.handle_session, None)  # game loads: lock-in view stays up
        await asyncio.sleep(interval * 2)
        live.handle_phase("Lobby", None)  # back in the lobby: screen clears
        await asyncio.sleep(interval * 2)


def _band(ban: float, neutral: float) -> str:
    if ban > neutral:
        return "positive"
    if ban < -neutral:
        return "negative"
    return "neutral"


def lane_ban_report(engine: DraftEngine) -> list[dict]:
    """The Prep page's per-role tables: mu, then Opp / PR / Resp / via / Ban sorted by Ban."""
    tables: dict[str, RoleBanTable] = engine.lane_ban_tables(DraftState(TOURNAMENT, "blue"))
    neutral = engine.cfg.lane_ban.neutral_band
    name = engine.data.name
    return [
        {
            "role": r,
            "role_label": ROLE_LABELS[r],
            "player_bracket": engine.team.brackets[r],
            "mu": round(tables[r].mu, 2),
            "rows": [
                {
                    "opp_id": row.opp,
                    "opp": name(row.opp),
                    "pr": round(100 * row.pick_rate, 1),
                    "resp": round(row.resp, 1),
                    "via_id": row.via,
                    "via": name(row.via) if row.via is not None else row.via_note,
                    "ban": round(row.ban, 2),
                    "band": _band(round(row.ban, 2), neutral),  # matches the displayed value
                    "degenerate": row.degenerate,
                }
                for row in tables[r].rows
            ],
        }
        for r in ROLES
    ]


def create_app(services: Services | None = None, web_dist: Path = WEB_DIST) -> FastAPI:
    """`services` is injected by tests; otherwise it is built from settings at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services(load_settings())
        app.state.services = svc
        svc.hub.bind(asyncio.get_running_loop())
        demo_task = None
        if svc.lcu_mode == "client":
            from draftassist.lcu.connector import LcuConnector

            LcuConnector(svc.live.handle_session, svc.live.handle_phase, svc.live.handle_status).start()
        elif svc.lcu_mode == "demo":
            demo_task = asyncio.create_task(run_demo(svc.live))
        yield
        if demo_task:
            demo_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await demo_task

    app = FastAPI(title="Draft Assistant", lifespan=lifespan)

    @app.get("/api/live")
    def live_state(request: Request) -> dict:
        return request.app.state.services.hub.latest

    @app.websocket("/ws/live")
    async def live_ws(ws: WebSocket) -> None:
        await ws.app.state.services.hub.serve(ws)

    @app.get("/api/prep/lane-bans")
    def prep_lane_bans(request: Request) -> dict:
        svc: Services = request.app.state.services
        return {
            "data_source": svc.data_source,
            "note": svc.note,
            "enemy_bracket": svc.engine.cfg.enemy_bracket,
            "roles": lane_ban_report(svc.engine),
        }

    if (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return ("<h1>Draft Assistant</h1><p>The web UI isn't built yet: run "
                    "<code>npm install &amp;&amp; npm run build</code> in <code>web/</code>.</p>")

    return app


app = create_app()
