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
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from sqlmodel import Session

from draftassist.api.draftlog import DraftLogger
from draftassist.api.history_routes import router as history_router
from draftassist.api.learning_routes import router as learning_router
from draftassist.api.live import LiveHub, LiveService
from draftassist.api.pool_routes import router as pool_router
from draftassist.api.settings_routes import router as settings_router
from draftassist.api.synergy_routes import router as synergy_router
from draftassist.config import PROJECT_ROOT, Settings, load_settings
from draftassist.data import ddragon
from draftassist.data.pools import (
    PlayerDirectoryDb,
)
from draftassist.data.profiles import load_profiles
from draftassist.data.provider import ChampionCatalog, load_catalog, load_engine_inputs, stats_version
from draftassist.data.rules import STYLE_RULES_FILE, TEAM_RULES_FILE
from draftassist.data.settings_store import (
    engine_config,
    get_synergy_bracket,
    load_pinned,
)
from draftassist.data.synergy_model import build_evaluator
from draftassist.data.tags import TAGS_FILE, sync_tags
from draftassist.db.session import init_db
from draftassist.engine.engine import DraftEngine
from draftassist.engine.formats import get_format
from draftassist.lcu.demo import demo_sessions
from draftassist.lcu.recorder import SessionRecorder
from draftassist.learning.jobs import LearningResult, run_in_background

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
    loaded_version: tuple | None = None  # stats_version() the engine was built from
    learning: dict | None = None  # last learning run: {"running", "finished_at", "summary"}
    tags_path: Path = TAGS_FILE  # judgement tags (docs/synergy-v2.md); tests point it at a scratch copy
    style_rules_path: Path = STYLE_RULES_FILE
    team_rules_path: Path = TEAM_RULES_FILE
    synergy_v2_override: object | None = None  # tests: a fixed SynergyEvaluator
    pinned: dict | None = None  # the plan pinned for the next game (docs/synergy-v2.md, Stage 6)

    def __post_init__(self) -> None:
        if self.db is not None and self.pinned is None:
            self.pinned = load_pinned(self.db)
        self.live.pin_provider = lambda: self.pinned  # read on every champ select event
        if self.live.logger is not None:
            self.live.logger.pin_provider = lambda: self.pinned
    _synergy_v2: object | None = None  # built lazily, dropped on reload and after tag / rule edits
    _synergy_pending: object | None = None  # a rebuilt evaluator waiting for the draft to end

    def synergy_v2(self):
        """The synergy v2 evaluator (Stat / Style / Team), warmed for our pools: the one the engine scores
        Live with when it has one, else built on demand."""
        if self.synergy_v2_override is not None:
            return self.synergy_v2_override
        if self.engine.data.synergy_v2 is not None:
            return self.engine.data.synergy_v2
        if self._synergy_v2 is None:
            self._synergy_v2 = self._build_synergy_v2(self.engine.data.synergy, self.engine.team.pools)
        return self._synergy_v2

    def _build_synergy_v2(self, stat, pools):
        from draftassist.engine.style import StyleModel
        from draftassist.engine.synergy_v2 import SynergyEvaluator
        from draftassist.engine.team import TeamModel
        from draftassist.engine.traits import EMPTY_TRAITS

        if self.db is None:
            return SynergyEvaluator(stat, EMPTY_TRAITS, StyleModel([], {}), TeamModel([], EMPTY_TRAITS))
        return build_evaluator(self.db, stat, get_synergy_bracket(self.db), self.style_rules_path,
                               self.team_rules_path, pools)

    def invalidate_synergy(self) -> None:
        """After a tag, rule or weight edit: rebuild the evaluator. The engine's copy (Live scoring) is
        swapped at once between drafts, else at the next stats refresh after the draft."""
        self._synergy_v2 = None
        if self.engine.data.synergy_v2 is None or self.db is None:
            return
        fresh = self._build_synergy_v2(self.engine.data.synergy, self.engine.team.pools)
        if self.in_draft():
            self._synergy_pending = fresh
        else:
            self._swap_synergy(fresh)

    def _swap_synergy(self, ev) -> None:
        from dataclasses import replace as _replace

        self.engine.data = _replace(self.engine.data, synergy_v2=ev)
        self._synergy_pending = None

    def profiles(self):
        """Champion profiles (timing, fight style from the tags) for the current patch, read fresh."""
        patch = ".".join(self.catalog.patch.split(".")[:2]) if self.catalog.patch else None
        return load_profiles(self.db, patch)

    def refresh_profiles(self) -> bool:
        """After a style review: give the engine the new champion profiles, but not mid-draft."""
        if self.db is None or self.in_draft():
            return False
        from dataclasses import replace

        patch = ".".join(self.catalog.patch.split(".")[:2]) if self.catalog.patch else None
        self.engine.data = replace(self.engine.data, profiles=load_profiles(self.db, patch))
        return True

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
        version = stats_version(self.db)
        inputs = load_engine_inputs(self.db)
        live = self.live
        data = with_synergy_v2(self.db, inputs, self.style_rules_path, self.team_rules_path)
        live.engine, live.catalog = DraftEngine(data, inputs.team, engine_config(self.db)), inputs.catalog
        live.data_source, live.note = inputs.data_source, inputs.note
        self._synergy_v2 = None
        if self.players is not None:
            self.players.invalidate()
        self.loaded_version = version

    def make_riot(self):
        if self.settings is None or not self.settings.has_riot_api_key:
            return None
        from draftassist.data.riot import RiotClient

        settings = self.settings
        return lambda: RiotClient(settings.riot_api_key, settings.cache_dir)

    def learn(self) -> bool:
        """Start the learning job (link players, collect games, re-score tiers) in the background.
        False if one is already running. The engine reloads between drafts when it is done."""
        if self.db is None or (self.learning or {}).get("running"):
            return False
        import time as _time

        self.learning = {**(self.learning or {}), "running": True}

        def done(res: LearningResult) -> None:
            self.learning = {"running": False, "finished_at": _time.time(), "summary": {
                "logged_games": res.logged_games, "history_games": res.history_games,
                "proposals": len(res.proposals), "history_error": res.history_error}}
            self.refresh_stats()

        def failed() -> None:
            self.learning = {**(self.learning or {}), "running": False, "summary": {"error": "learning job failed (see log)"}}

        run_in_background(self.db, self.make_riot(), done, failed)
        return True

    def in_draft(self) -> bool:
        """True while a champ select is running (suggestions must not change under the shot-caller)."""
        latest = self.hub.latest
        return self.live.gameflow_phase == "ChampSelect" or (
            latest.get("status") == "champ_select" and latest.get("phase") != "Locked in")

    def refresh_stats(self) -> str:
        """Reload stats if the crawl (or anything else) changed them, but only between drafts.
        Returns 'unchanged', 'deferred' (in a draft; retried next time) or 'reloaded'."""
        if self._synergy_pending is not None and not self.in_draft():
            self._swap_synergy(self._synergy_pending)
        if self.db is None or stats_version(self.db) == self.loaded_version:
            return "unchanged"
        if self.in_draft():
            return "deferred"
        self.reload()
        log.info("stats reloaded: %s", self.live.note or "real stats and pools")
        if self.hub.latest.get("status") != "champ_select":
            self.hub.publish_threadsafe(self.live._waiting())  # banner shows the new sources
        return "reloaded"


def with_synergy_v2(db: Engine, inputs, style_path: Path = STYLE_RULES_FILE, team_path: Path = TEAM_RULES_FILE):
    """The engine's data with the synergy v2 evaluator attached, so Live scores Stat + Style + Team
    (docs/synergy-v2.md). Synthetic fixture stats keep the v1 term."""
    from dataclasses import replace as _replace

    if inputs.data_source == "fixture":
        return inputs.data
    try:
        ev = build_evaluator(db, inputs.data.synergy, get_synergy_bracket(db), style_path, team_path,
                             inputs.team.pools)
    except (OSError, ValueError) as e:  # a broken rule file must not stop the app: v1 synergy until fixed
        log.warning("synergy v2 unavailable, Live uses the v1 synergy term: %s", e)
        return inputs.data
    return _replace(inputs.data, synergy_v2=ev)


def build_services(settings: Settings) -> Services:
    db = init_db(settings.db_path)
    if settings.has_riot_api_key:
        from draftassist.data.riot import sync_api_key

        sync_api_key(db, settings.riot_api_key)  # a new key: PUUIDs from the old one are useless
    if not load_catalog(db).champs:
        try:  # first run: fetch champion names/icons so live drafts show real champions
            with httpx.Client(timeout=30) as client, Session(db) as session:
                ddragon.refresh(client, session, settings.cache_dir, settings.ddragon_locale)
        except httpx.HTTPError:
            log.warning("could not load Data Dragon; champion names will be ids")
    try:
        sync_tags(db, TAGS_FILE)  # the YAML file is the source of truth for judgement tags
    except (OSError, ValueError) as e:
        log.warning("could not load %s: %s", TAGS_FILE, e)
    inputs = load_engine_inputs(db)
    engine = DraftEngine(with_synergy_v2(db, inputs), inputs.team, engine_config(db))
    hub = LiveHub()
    players = PlayerDirectoryDb(db)
    client = settings.lcu_mode == "client"
    recorder = SessionRecorder(RECORDINGS) if settings.record_sessions and client else None
    logger = DraftLogger(db) if client or settings.demo_log else None

    services_ref: list[Services] = []

    def verify_later(draft_id: int) -> None:
        if services_ref:
            services_ref[0].learn()  # tiers update after each logged game
        if client and settings.has_riot_api_key:
            from draftassist.data.postgame import schedule_verification
            from draftassist.data.riot import RiotClient

            schedule_verification(db, lambda: RiotClient(settings.riot_api_key, settings.cache_dir), draft_id)

    live = LiveService(engine, inputs.catalog, hub, inputs.data_source, inputs.note, settings.draft_format,
                       recorder, logger, players, verify_later)
    if logger is not None:
        logger.context = lambda: (live.engine, live.catalog)
    svc = Services(hub, live, db, players, settings.lcu_mode, settings, stats_version(db))
    services_ref.append(svc)
    return svc


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


async def refresh_stats_forever(svc: Services, every_s: float) -> None:
    while True:
        await asyncio.sleep(every_s)
        try:
            await asyncio.to_thread(svc.refresh_stats)
        except Exception:
            log.exception("stats refresh failed; keeping the current stats")


async def run_replay(live: LiveService, folder: Path, interval: float = 1.5) -> None:
    """Replay a recorded champ select (fixtures/lcu/...) through the live pipeline, on a loop."""
    import json

    events = sorted(folder.glob("*-session.json"))
    if not events:
        live.handle_status(f"replay: no recorded sessions in {folder}")
        return
    while True:
        live.handle_status(f"replay: {folder.name}")
        live.handle_phase("ChampSelect", None)
        for path in events:
            await asyncio.to_thread(live.handle_session, json.loads(path.read_text(encoding="utf-8")))
            await asyncio.sleep(interval)
        await asyncio.sleep(interval * 3)
        live.handle_phase("Lobby", None)
        await asyncio.sleep(interval * 2)


class UIFiles(StaticFiles):
    """The built UI. index.html is revalidated on every load so an updated app never runs a stale
    bundle; the hashed asset files can be cached."""

    def file_response(self, full_path, *args, **kwargs):
        response = super().file_response(full_path, *args, **kwargs)
        if str(full_path).endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        return response




def start_live_feed(svc: Services) -> asyncio.Task | None:
    """Start what feeds the Live screen for `svc.lcu_mode`: the League client (its own thread), the demo
    draft loop, or a recorded replay. Returns the asyncio task to cancel on shutdown, if any."""
    if svc.lcu_mode == "client":
        from draftassist.lcu.connector import LcuConnector

        live = svc.live
        LcuConnector(live.handle_session, live.handle_phase, live.handle_status, live.handle_end_of_game).start()
    elif svc.lcu_mode == "demo":
        return asyncio.create_task(run_demo(svc.live))
    elif svc.lcu_mode == "replay" and svc.settings is not None:
        folder = Path(svc.settings.replay_dir or PROJECT_ROOT / "fixtures" / "lcu" / "flex-ranked-sample")
        return asyncio.create_task(run_replay(svc.live, folder))
    return None


def create_app(services: Services | None = None, web_dist: Path = WEB_DIST,
               stats_refresh_s: float | None = None) -> FastAPI:
    """`services` is injected by tests; otherwise it is built from settings at startup.
    Stats are re-checked every `stats_refresh_s` seconds (default: settings; 0 = never)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services(load_settings())
        app.state.services = svc
        svc.hub.bind(asyncio.get_running_loop())
        if services is None and svc.lcu_mode == "client":
            svc.learn()  # catch up on games played since the last run
        every = stats_refresh_s if stats_refresh_s is not None else (svc.settings.stats_refresh_s if svc.settings else 0)
        refresh_task = asyncio.create_task(refresh_stats_forever(svc, every)) if every > 0 else None
        demo_task = start_live_feed(svc)
        yield
        for task in (demo_task, refresh_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Draft Assistant", lifespan=lifespan)

    @app.get("/api/live")
    def live_state(request: Request) -> dict:
        return request.app.state.services.hub.latest

    @app.websocket("/ws/live")
    async def live_ws(ws: WebSocket) -> None:
        await ws.app.state.services.hub.serve(ws)

    for router in (pool_router, settings_router, synergy_router, learning_router, history_router):
        app.include_router(router)

    if (web_dist / "index.html").is_file():
        app.mount("/", UIFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return ("<h1>Draft Assistant</h1><p>The web UI isn't built yet: run "
                    "<code>npm install &amp;&amp; npm run build</code> in <code>web/</code>.</p>")

    return app


app = create_app()
