"""FastAPI app: Live screen (WebSocket push), Pools, History, Prep lane-ban tables, and the web UI.

Run: `uv run uvicorn draftassist.api.main:app --reload`, then open http://localhost:8000
Live feed: DRAFTASSIST_LCU=client (default, the real League client) | demo (simulated draft) | off
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from sqlmodel import Session

from draftassist.api.draftlog import DraftLogger
from draftassist.api.history import history
from draftassist.api.live import LiveHub, LiveService
from draftassist.config import PROJECT_ROOT, Settings, load_settings
from draftassist.data import ddragon
from draftassist.data.pools import (
    PlayerDirectoryDb,
    import_pools,
    list_players,
    load_pools,
    parse_pool_text,
    set_players,
)
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
DEMO_GAME_ID_BASE = 9_000_000_000  # demo games get ids far from real EUW game ids


@dataclass
class Services:
    hub: LiveHub
    live: LiveService
    db: Engine | None = None
    players: PlayerDirectoryDb | None = None
    lcu_mode: str = "off"
    settings: Settings | None = None

    @property
    def engine(self) -> DraftEngine:
        return self.live.engine

    @property
    def catalog(self) -> ChampionCatalog:
        return self.live.catalog

    @property
    def data_source(self) -> str:
        return self.live.data_source

    @property
    def note(self) -> str | None:
        return self.live.note

    def reload(self) -> None:
        """Rebuild the engine after pools or players change (e.g. a pool import)."""
        if self.db is None:
            return
        inputs = load_engine_inputs(self.db)
        live = self.live
        live.engine, live.catalog = DraftEngine(inputs.data, inputs.team), inputs.catalog
        live.data_source, live.note = inputs.data_source, inputs.note
        if self.players is not None:
            self.players.invalidate()


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
    players = PlayerDirectoryDb(db)
    client = settings.lcu_mode == "client"
    recorder = SessionRecorder(RECORDINGS) if settings.record_sessions and client else None
    logger = DraftLogger(db) if client or settings.demo_log else None

    def verify_later(draft_id: int) -> None:
        if client and settings.has_riot_api_key:
            from draftassist.data.postgame import schedule_verification
            from draftassist.data.riot import RiotClient

            schedule_verification(db, lambda: RiotClient(settings.riot_api_key, settings.cache_dir), draft_id)

    live = LiveService(engine, inputs.catalog, hub, inputs.data_source, inputs.note, settings.draft_format,
                       recorder, logger, players, verify_later)
    return Services(hub, live, db, players, settings.lcu_mode, settings)


async def run_demo(live: LiveService, interval: float = 1.5, seed: int = 0) -> None:
    """Replay simulated drafts as LCU events, then the game starting and ending, forever."""
    rng = random.Random(seed)
    for game, side in enumerate(itertools.cycle(["blue", "red"]), start=1):
        game_id = DEMO_GAME_ID_BASE + game
        live.handle_status("demo mode (simulated draft)")
        live.handle_phase("ChampSelect", None)
        for session in demo_sessions(live.engine, get_format("tournament"), side, game_id=game_id):
            await asyncio.to_thread(live.handle_session, session)
            await asyncio.sleep(interval)
        await asyncio.sleep(interval * 2)
        live.handle_phase("GameStart", None)
        await asyncio.to_thread(live.handle_session, None)  # game loads: lock-in view stays up
        live.handle_phase("InProgress", None)
        await asyncio.sleep(interval * 2)
        won = rng.random() < 0.5
        team = 100 if side == "blue" else 200
        eog = {"gameId": game_id, "gameLength": 1700, "teams": [
            {"teamId": team, "isPlayerTeam": True, "isWinningTeam": won},
            {"teamId": 300 - team, "isPlayerTeam": False, "isWinningTeam": not won}]}
        live.handle_phase("EndOfGame", None)
        await asyncio.to_thread(live.handle_end_of_game, eog)
        await asyncio.sleep(interval)
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


def _db(svc: Services) -> Engine:
    if svc.db is None:
        raise HTTPException(503, "no database configured")
    return svc.db


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

            live = svc.live
            LcuConnector(live.handle_session, live.handle_phase, live.handle_status, live.handle_end_of_game).start()
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

    # ---------- players & pools ----------
    @app.get("/api/players")
    def get_players(request: Request) -> list[dict]:
        return list_players(_db(request.app.state.services))

    @app.put("/api/players")
    def put_players(request: Request, players: list[dict] = Body(..., embed=True)) -> list[dict]:
        svc: Services = request.app.state.services
        resolver = None
        if svc.settings is not None and svc.settings.has_riot_api_key:
            from draftassist.data.riot import RiotClient

            riot = RiotClient(svc.settings.riot_api_key, svc.settings.cache_dir)
            resolver = riot.account_puuid
        try:
            stored = set_players(_db(svc), players, resolver)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        svc.reload()
        return stored

    @app.get("/api/pools")
    def get_pools(request: Request) -> dict:
        svc: Services = request.app.state.services
        db = _db(svc)
        pools = load_pools(db)
        tier_order = {"main": 0, "comfortable": 1, "learning": 2}
        return {
            "players": list_players(db),
            "pools": {r: [{"champ": svc.catalog.card(e.champ_id), "manual_tier": e.manual_tier,
                           "scored_tier": e.scored_tier, "tier_locked": e.tier_locked, "games": e.games, "wins": e.wins}
                          for e in sorted(entries, key=lambda e: (tier_order.get(e.manual_tier, 9), e.champ_id))]
                      for r, entries in pools.items()},
            "using_placeholder_pools": not any(pools.values()),
        }

    @app.post("/api/pools/import")
    def post_pool_import(request: Request, text: str = Body(..., embed=True)) -> dict:
        svc: Services = request.app.state.services
        db = _db(svc)
        parsed = parse_pool_text(text, svc.catalog.champs)
        if not parsed.ok:
            raise HTTPException(422, {"errors": parsed.errors})
        counts = import_pools(db, parsed)
        svc.reload()
        return {"imported": counts}

    # ---------- history ----------
    @app.get("/api/history")
    def get_history(request: Request, limit: int = 50) -> list[dict]:
        svc: Services = request.app.state.services
        return history(_db(svc), svc.catalog, limit)

    if (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return ("<h1>Draft Assistant</h1><p>The web UI isn't built yet: run "
                    "<code>npm install &amp;&amp; npm run build</code> in <code>web/</code>.</p>")

    return app


app = create_app()
