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
from sqlmodel import Session, select

from draftassist.api.draftlog import DraftLogger
from draftassist.api.recap import history_filters, history_rows, recap
from draftassist.api.live import LiveHub, LiveService
from draftassist.api.synergy import (
    DEFAULT_THRESHOLD_PP,
    SynergySource,
    build_synergy_view,
    draft_context,
)
from draftassist.config import PROJECT_ROOT, Settings, load_settings
from draftassist.data import ddragon
from draftassist.data.pools import (
    PlayerDirectoryDb,
    import_pools,
    list_players,
    load_pools,
    parse_pool_text,
    remove_pool_entry,
    set_players,
    set_pool_entry,
)
from draftassist.data.provider import ChampionCatalog, load_catalog, load_engine_inputs, stats_version
from draftassist.data.profiles import load_profiles, reset_style, save_style
from draftassist.data.rules import STYLE_RULES_FILE, TEAM_RULES_FILE, set_prior
from draftassist.data.synergy_model import build_evaluator, fit_synergy_model, load_synergy_model
from draftassist.data.tags import TAGS_FILE, remove_tag, sync_tags, update_tag
from draftassist.data.traits import load_traits
from draftassist.engine.traits import TAGS
from draftassist.data.settings_store import (
    engine_config,
    get_synergy_bracket,
    save_ban_settings,
    save_engine_settings,
    save_synergy_bracket,
    settings_payload,
)
from draftassist.learning.comfort import accept as accept_tiers
from draftassist.learning.comfort import set_locked
from draftassist.learning.jobs import LearningResult, run_in_background
from draftassist.learning.prep import prep_sheet
from draftassist.learning.report import draft_report, model_check
from draftassist.learning.style import set_hand_style, team_summary
from draftassist.db.models import Champion
from draftassist.db.session import init_db
from draftassist.engine.engine import DraftEngine
from draftassist.engine.comp import STYLE_LABELS, STYLES, TIMING_LABELS, badge
from draftassist.engine.formats import TOURNAMENT, get_format
from draftassist.engine.lane_ban import RoleBanTable
from draftassist.engine.model import ROLE_LABELS, ROLES, BanWeights
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
    loaded_version: tuple | None = None  # stats_version() the engine was built from
    learning: dict | None = None  # last learning run: {"running", "finished_at", "summary"}
    synergy_override: SynergySource | None = None  # tests: a fixed table instead of the engine's
    tags_path: Path = TAGS_FILE  # judgement tags (docs/synergy-v2.md); tests point it at a scratch copy
    style_rules_path: Path = STYLE_RULES_FILE
    team_rules_path: Path = TEAM_RULES_FILE
    synergy_v2_override: object | None = None  # tests: a fixed SynergyEvaluator
    _synergy_v2: object | None = None  # built lazily, dropped on reload and after tag / rule edits

    def synergy_v2(self):
        """The synergy v2 evaluator (Stat / Style / Team), warmed for our pools."""
        if self.synergy_v2_override is not None:
            return self.synergy_v2_override
        if self._synergy_v2 is None:
            from draftassist.engine.style import StyleModel
            from draftassist.engine.synergy_v2 import SynergyEvaluator
            from draftassist.engine.team import TeamModel
            from draftassist.engine.traits import EMPTY_TRAITS

            stat = self.engine.data.synergy
            if self.db is None:
                self._synergy_v2 = SynergyEvaluator(stat, EMPTY_TRAITS, StyleModel([], {}), TeamModel([], EMPTY_TRAITS))
            else:
                self._synergy_v2 = build_evaluator(self.db, stat, get_synergy_bracket(self.db), self.style_rules_path,
                                                   self.team_rules_path, self.engine.team.pools)
        return self._synergy_v2

    def invalidate_synergy(self) -> None:
        self._synergy_v2 = None

    def synergy_source(self) -> SynergySource:
        """The engine's scored synergy pairs and damage profiles (reloaded with the stats)."""
        if self.synergy_override is not None:
            return self.synergy_override
        return SynergySource(self.engine.data.synergy, self.engine.data.damage, self.engine.data.profiles)

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
        live.engine, live.catalog = DraftEngine(inputs.data, inputs.team, engine_config(self.db)), inputs.catalog
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
        if self.db is None or stats_version(self.db) == self.loaded_version:
            return "unchanged"
        if self.in_draft():
            return "deferred"
        self.reload()
        log.info("stats reloaded: %s", self.live.note or "real stats and pools")
        if self.hub.latest.get("status") != "champ_select":
            self.hub.publish_threadsafe(self.live._waiting())  # banner shows the new sources
        return "reloaded"


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
    engine = DraftEngine(inputs.data, inputs.team, engine_config(db))
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


def _band(ban: float, neutral: float) -> str:
    if ban > neutral:
        return "positive"
    if ban < -neutral:
        return "negative"
    return "neutral"


def lane_ban_report(engine: DraftEngine) -> list[dict]:
    """The Prep page's per-role tables: mu, then Opp / PR / Resp / via / Ban sorted by Ban, plus each
    opponent's final team ban value (all components), to compare the lane view with the ranking."""
    empty = DraftState(TOURNAMENT, "blue")
    tables: dict[str, RoleBanTable] = engine.lane_ban_tables(empty)
    team_value = {r["champ_id"]: r["value"] for r in engine.ban_breakdown(empty)}
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
                    "team_ban_value": round(team_value[row.opp], 4) if row.opp in team_value else None,
                }
                for row in tables[r].rows
            ],
        }
        for r in ROLES
    ]


class UIFiles(StaticFiles):
    """The built UI. index.html is revalidated on every load so an updated app never runs a stale
    bundle; the hashed asset files can be cached."""

    def file_response(self, full_path, *args, **kwargs):
        response = super().file_response(full_path, *args, **kwargs)
        if str(full_path).endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def _db(svc: Services) -> Engine:
    if svc.db is None:
        raise HTTPException(503, "no database configured")
    return svc.db


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
        demo_task = None
        every = stats_refresh_s if stats_refresh_s is not None else (svc.settings.stats_refresh_s if svc.settings else 0)
        refresh_task = asyncio.create_task(refresh_stats_forever(svc, every)) if every > 0 else None
        if svc.lcu_mode == "client":
            from draftassist.lcu.connector import LcuConnector

            live = svc.live
            LcuConnector(live.handle_session, live.handle_phase, live.handle_status, live.handle_end_of_game).start()
        elif svc.lcu_mode == "demo":
            demo_task = asyncio.create_task(run_demo(svc.live))
        elif svc.lcu_mode == "replay" and svc.settings is not None:
            folder = Path(svc.settings.replay_dir or PROJECT_ROOT / "fixtures" / "lcu" / "flex-ranked-sample")
            demo_task = asyncio.create_task(run_replay(svc.live, folder))
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
        return _pools_payload(request.app.state.services)

    def _pool_changed(svc: Services) -> dict:
        """After an edit: rebuild the engine (not mid-draft: suggestions must not change under the shot-caller)."""
        applied = not svc.in_draft()
        if applied:
            svc.reload()
        return {**_pools_payload(svc), "applied": applied}

    @app.put("/api/pools/{role}/{champ_id}")
    def put_pool_entry(request: Request, role: str, champ_id: int, tier: str = Body(..., embed=True)) -> dict:
        """Add a champion to a player's pool, or change its tier."""
        svc: Services = request.app.state.services
        if champ_id not in svc.catalog.champs:
            raise HTTPException(404, "unknown champion")
        try:
            set_pool_entry(_db(svc), role, champ_id, tier)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return _pool_changed(svc)

    @app.delete("/api/pools/{role}/{champ_id}")
    def delete_pool_entry(request: Request, role: str, champ_id: int) -> dict:
        svc: Services = request.app.state.services
        if not remove_pool_entry(_db(svc), role, champ_id):
            raise HTTPException(404, "not in that pool")
        return _pool_changed(svc)

    def _pools_payload(svc: Services) -> dict:
        db = _db(svc)
        pools = load_pools(db)
        profiles = _profiles(svc)
        tier_order = {"main": 0, "comfortable": 1, "learning": 2}
        return {
            "players": list_players(db),
            "learning": svc.learning or {"running": False},
            "pools": {r: [{"champ": svc.catalog.card(e.champ_id), "manual_tier": e.manual_tier,
                           "badge": badge(profiles, e.champ_id, r).as_dict(),
                           "scored_tier": e.scored_tier, "tier_locked": e.tier_locked, "games": e.games, "wins": e.wins,
                           "comfort": round(e.comfort, 2),
                           "last_played": e.last_played.isoformat() if e.last_played else None,
                           "proposal": (e.scored_tier if e.scored_tier and e.scored_tier != e.manual_tier
                                        and not e.tier_locked else None)}
                          for e in sorted(entries, key=lambda e: (tier_order.get(e.manual_tier, 9), e.champ_id))]
                      for r, entries in pools.items()},
            "using_placeholder_pools": not any(pools.values()),
            "champions": sorted(({"id": c, "name": name} for c, (_, name) in svc.catalog.champs.items()),
                                key=lambda c: c["name"]),
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

    # ---------- settings ----------
    @app.get("/api/settings")
    def get_settings(request: Request) -> dict:
        return settings_payload(_db(request.app.state.services))

    @app.put("/api/settings")
    def put_settings(request: Request, ban_weights: dict = Body(...), ban_declared_intents: bool = Body(False),
                     alpha: float | None = Body(None), beta: float | None = Body(None),
                     comfort_games_cap: int | None = Body(None), damage_balance: bool | None = Body(None),
                     damage_penalty: float | None = Body(None), ban_comp_synergy: bool | None = Body(None),
                     comp_coherence: bool | None = Body(None), comp_weight: float | None = Body(None)) -> dict:
        svc: Services = request.app.state.services
        db = _db(svc)
        try:
            weights = BanWeights(float(ban_weights["threat"]), float(ban_weights["matchup"]),
                                 float(ban_weights["comp"]), float(ban_weights.get("own", BanWeights().own)))
            save_ban_settings(db, weights, ban_declared_intents)
            save_engine_settings(db, alpha=alpha, beta=beta, comfort_games_cap=comfort_games_cap,
                                 damage_balance=damage_balance, damage_penalty=damage_penalty,
                                 ban_comp_synergy=ban_comp_synergy, comp_coherence=comp_coherence,
                                 comp_weight=comp_weight)
            if comfort_games_cap is not None:
                from draftassist.learning.comfort import rescore

                rescore(db)  # stored comfort shown on the Pools page uses the new cap
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid settings: {e}") from None
        svc.reload()
        return settings_payload(db)

    @app.put("/api/settings/synergy")
    def put_synergy_settings(request: Request, values: dict[str, float] = Body(..., embed=True)) -> dict:
        """Synergy v2 weights (Overall = Stat / Style / Team), confidence k, prior SD of the fits, ban d."""
        from draftassist.data.settings_store import save_synergy_v2_settings

        svc: Services = request.app.state.services
        db = _db(svc)
        try:
            save_synergy_v2_settings(db, **values)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, str(e)) from None
        if "prior_sd_pp" in values:
            fit_synergy_model(db, style_path=svc.style_rules_path, team_path=svc.team_rules_path)
        svc.invalidate_synergy()
        return settings_payload(db)

    # ---------- synergy ----------
    @app.post("/api/synergy")
    def post_synergy(request: Request, roles: list[str] = Body(...), columns: dict[str, list[int]] | None = Body(None),
                     locks: dict[str, int] | None = Body(None), threshold_pp: float = Body(DEFAULT_THRESHOLD_PP),
                     use_draft: bool = Body(False)) -> dict:
        """The Synergy page's graph. Read-only; POST only because the selection is a JSON body."""
        svc: Services = request.app.state.services
        if not 2 <= len(roles) <= 5 or len(set(roles)) != len(roles) or not set(roles) <= set(ROLES):
            raise HTTPException(422, f"choose 2 to 5 distinct roles from {', '.join(ROLES)}")
        for r in (*(columns or {}), *(locks or {})):
            if r not in ROLES:
                raise HTTPException(422, f"unknown role {r!r}")
        engine, live = svc.engine, svc.live.current
        unavailable: set[int] = set()
        all_locks = dict(locks or {})
        draft = {"live": False}
        if use_draft and live is not None:
            ev = engine.evaluate(live.state)
            unavailable, draft_locks = draft_context(live, ev.our_roles)
            all_locks |= draft_locks
            draft = {"live": True, "locks": draft_locks, "unavailable": sorted(unavailable)}
        players = {p["role"]: p["riot_id"] for p in list_players(svc.db)} if svc.db is not None else {}
        view = build_synergy_view(svc.synergy_source(), svc.catalog, engine.team, roles, columns, all_locks,
                                  threshold_pp, unavailable, players)
        champs = sorted((c for c in (svc.catalog.card(i) for i in svc.catalog.champs) if c), key=lambda c: c["name"])
        return {**view, "draft": draft, "champions": champs,
                "synergy_bracket": get_synergy_bracket(svc.db) if svc.db is not None else "all"}

    @app.put("/api/synergy/bracket")
    def put_synergy_bracket(request: Request, bracket: str = Body(..., embed=True)) -> dict:
        svc: Services = request.app.state.services
        try:
            save_synergy_bracket(_db(svc), bracket)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if svc.in_draft():
            return {"synergy_bracket": bracket, "applied": False}  # suggestions must not change mid-draft
        svc.reload()  # the engine's synergy term uses the same table
        return {"synergy_bracket": bracket, "applied": True}

    # ---------- champion styles (docs/comp-style.md) ----------
    def _style_row(svc: Services, champ: Champion, profiles) -> dict:
        st = profiles.style(champ.champ_id)
        timing = {}
        for role in champ.roles or []:
            tc = profiles.curve(champ.champ_id, role)
            timing[role] = None if tc is None else {"curve": round(tc.curve, 3), "games": tc.games, "label": tc.label}
        return {"champ": svc.catalog.card(champ.champ_id), "roles": champ.roles or [], "tags": champ.tags or [],
                "weights": {k: round(v, 3) for k, v in st.as_dict().items()}, "frontline": st.frontline,
                "source": st.source, "reviewed": st.reviewed, "unsure": st.unsure, "timing": timing}

    def _profiles(svc: Services):
        patch = ".".join(svc.catalog.patch.split(".")[:2]) if svc.catalog.patch else None
        return load_profiles(_db(svc), patch)

    @app.get("/api/champions/styles")
    def get_champion_styles(request: Request) -> dict:
        svc: Services = request.app.state.services
        profiles = _profiles(svc)
        with Session(_db(svc)) as s:
            champs = sorted(s.exec(select(Champion)).all(), key=lambda c: c.name)
        rows = [_style_row(svc, c, profiles) for c in champs]
        return {"champions": rows, "total": len(rows), "reviewed": sum(r["reviewed"] for r in rows),
                "styles": STYLE_LABELS, "timings": TIMING_LABELS}

    def _one(svc: Services, champ_id: int) -> dict:
        with Session(_db(svc)) as s:
            champ = s.get(Champion, champ_id)
        if champ is None:
            raise HTTPException(404, "unknown champion")
        return _style_row(svc, champ, _profiles(svc))

    @app.put("/api/champions/styles/{champ_id}")
    def put_champion_style(request: Request, champ_id: int, weights: dict[str, float] | None = Body(None),
                           frontline: bool | None = Body(None)) -> dict:
        """Save the user's review; weights null = confirm the seed as is."""
        svc: Services = request.app.state.services
        _one(svc, champ_id)
        try:
            w = None if weights is None else tuple(float(weights[k]) for k in STYLES)
            save_style(_db(svc), champ_id, w, frontline)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid style: {e}") from None
        svc.refresh_profiles()
        return _one(svc, champ_id)

    @app.delete("/api/champions/styles/{champ_id}")
    def delete_champion_style(request: Request, champ_id: int) -> dict:
        """Back to the seed (or Data Dragon classes)."""
        svc: Services = request.app.state.services
        reset_style(_db(svc), champ_id)
        svc.refresh_profiles()
        return _one(svc, champ_id)

    # ---------- judgement tags (docs/synergy-v2.md, Stage 1b) ----------
    def _tag_row(svc: Services, champ: Champion, traits) -> dict:
        entries = {role: {"values": dict(e.values), "reviewed": e.reviewed}
                   for (c, role), e in traits.tags.items() if c == champ.champ_id}
        measured = {role: {k: (None if v is None else round(v, 3)) for k, v in m.items()}
                    for (c, role), m in traits.measured.items() if c == champ.champ_id}
        return {"champ_id": champ.champ_id, "champ": svc.catalog.card(champ.champ_id), "key": champ.key, "name": champ.name, "roles": champ.roles or [],
                "attack_range": champ.attack_range, "entries": entries, "measured": measured,
                "untagged": not entries}

    def _tag_one(svc: Services, champ_id: int) -> dict:
        with Session(_db(svc)) as s:
            champ = s.get(Champion, champ_id)
        if champ is None:
            raise HTTPException(404, "unknown champion")
        return _tag_row(svc, champ, load_traits(_db(svc)))

    @app.get("/api/tags")
    def get_tags(request: Request) -> dict:
        svc: Services = request.app.state.services
        traits = load_traits(_db(svc))
        with Session(_db(svc)) as s:
            champs = sorted(s.exec(select(Champion)).all(), key=lambda c: c.name)
        rows = [_tag_row(svc, c, traits) for c in champs]
        entries = [e for r in rows for e in r["entries"].values()]
        return {"tags": list(TAGS), "roles": list(ROLES), "role_labels": ROLE_LABELS,
                "version": svc.catalog.patch, "champions": rows,
                "counts": {"champions": len(rows), "entries": len(entries),
                           "reviewed": sum(e["reviewed"] for e in entries),
                           "untagged": sum(r["untagged"] for r in rows)}}

    @app.put("/api/tags/{champ_id}/{role}")
    def put_tag(request: Request, champ_id: int, role: str, values: dict[str, int] = Body(...),
                reviewed: bool = Body(...)) -> dict:
        svc: Services = request.app.state.services
        _tag_one(svc, champ_id)
        try:
            update_tag(_db(svc), svc.tags_path, champ_id, role, values, reviewed)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        svc.invalidate_synergy()
        return _tag_one(svc, champ_id)

    @app.delete("/api/tags/{champ_id}/{role}")
    def delete_tag(request: Request, champ_id: int, role: str) -> dict:
        svc: Services = request.app.state.services
        remove_tag(_db(svc), svc.tags_path, champ_id, role)
        svc.invalidate_synergy()
        return _tag_one(svc, champ_id)

    # ---------- synergy v2 rules (docs/synergy-v2.md, Stage 2) ----------
    def _rules_payload(svc: Services) -> dict:
        from draftassist.data.settings_store import get_synergy_v2_settings
        from draftassist.engine.style import CODE_OF, flagged

        db = _db(svc)
        model = load_synergy_model(db, style_path=svc.style_rules_path, team_path=svc.team_rules_path)

        def row(kind: str, rule, extra: dict) -> dict:
            f = model.fits.get((kind, rule.id))
            fitted = None if f is None or f.prior_pp != rule.prior_pp else round(25 * f.value, 2)
            return {"id": rule.id, "note": rule.note, "prior_pp": rule.prior_pp, "fitted_pp": fitted,
                    "support": f.support if f else 0,
                    "flagged": fitted is not None and flagged(rule.prior_pp, fitted), **extra}

        style = [row("style", r, {"pair": r.pair, "when": {CODE_OF[role]: t for role, t in r.when}})
                 for r in model.style.rules]
        team = [row("team", r, {"cross": r.cross, "feature": r.feature, "ours": r.ours, "theirs": r.theirs})
                for r in model.team.rules]
        return {"window": model.window, "matches": model.matches,
                "prior_sd_pp": get_synergy_v2_settings(db)["prior_sd_pp"], "style": style, "team": team}

    @app.get("/api/rules")
    def get_rules(request: Request) -> dict:
        return _rules_payload(request.app.state.services)

    @app.put("/api/rules")
    def put_rule(request: Request, kind: str = Body(...), id: str = Body(...), prior_pp: float = Body(...)) -> dict:
        """Edit one rule's prior: written back to its YAML file, then every rule is refitted."""
        svc: Services = request.app.state.services
        try:
            set_prior(kind, id, prior_pp, svc.style_rules_path, svc.team_rules_path)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        fit_synergy_model(_db(svc), style_path=svc.style_rules_path, team_path=svc.team_rules_path)
        svc.invalidate_synergy()
        rows = _rules_payload(svc)["style" if kind == "style" else "team"]
        return next(r for r in rows if r["id"] == id)

    @app.post("/api/rules/refit")
    def post_refit(request: Request) -> dict:
        svc: Services = request.app.state.services
        try:
            result = fit_synergy_model(_db(svc), style_path=svc.style_rules_path, team_path=svc.team_rules_path)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        svc.invalidate_synergy()
        return result

    # ---------- synergy v2: evaluate (docs/synergy-v2.md, Stage 3) ----------
    def _comp_input(svc: Services, champions: list[dict], enemies: list[dict] | None):
        comp: dict[str, int] = {}
        for item in champions:
            role, champ = item.get("role"), item.get("champ_id")
            if role not in ROLES or not isinstance(champ, int) or role in comp:
                raise HTTPException(422, "champions: one {champ_id, role} per role, roles from " + ", ".join(ROLES))
            comp[role] = champ
        if len(set(comp.values())) != len(comp):
            raise HTTPException(422, "a champion can only fill one role")
        enemies = enemies or []
        if len(enemies) > 5:
            raise HTTPException(422, "at most 5 enemy champions")
        known = {e["champ_id"]: e["role"] for e in enemies if e.get("role") in ROLES}
        rest = [e["champ_id"] for e in enemies if e.get("role") not in ROLES]
        if rest:
            free = tuple(r for r in ROLES if r not in known.values())
            guess = svc.engine._assign("them", tuple(rest), free)[1] if len(rest) <= len(free) else {}
            known |= guess
        return comp, [(c, r) for c, r in known.items()]

    @app.post("/api/synergy/evaluate")
    def post_synergy_evaluate(request: Request, champions: list[dict] = Body(...), enemies: list[dict] | None = Body(None),
                              banned: list[int] | None = Body(None), k: int = Body(5)) -> dict:
        """Stat / Style / Team for up to 5 of our champions (any roles) and optional enemies; for missing
        roles, the top k pool champions by Overall gain. Banned and picked champions are never suggested."""
        from draftassist.api.synergy_v2 import evaluation_json
        from draftassist.engine.engine import Suggestion as EngineSuggestion
        from draftassist.engine.formats import TOURNAMENT as _T
        from draftassist.engine.guards import validate_suggestions

        svc: Services = request.app.state.services
        comp, enemy = _comp_input(svc, champions, enemies)
        ev = svc.synergy_v2()
        team = svc.engine.team
        unavailable = set(banned or []) | {c for c, _ in enemy}
        result = ev.evaluate(comp, enemy)
        suggestions = ev.suggest(comp, {r: list(team.pools.get(r, {})) for r in ROLES}, unavailable, enemy, k=k)
        # last check, as for every suggestion list: the single availability source and Rule 1
        state = DraftState(_T, "blue", provisional=frozenset(unavailable | set(comp.values())))
        for role, rows in suggestions.items():
            checked = validate_suggestions(state, "pick", [
                EngineSuggestion(s.champ, svc.catalog.card(s.champ)["name"], "pick", s.gain, "", role) for s in rows])
            keep = {s.champ_id for s in checked}
            suggestions[role] = [s for s in rows if s.champ in keep]
        tier = lambda c, r: (e.tier if (e := team.entry(c, r)) else None)  # noqa: E731
        return {"window": ev.stat.window, **evaluation_json(result, svc.catalog.card, suggestions, tier)}

    # ---------- learning ----------
    @app.post("/api/pools/accept")
    def post_accept(request: Request, role: str = Body(...), champ_id: int | None = Body(None)) -> dict:
        svc: Services = request.app.state.services
        changed = accept_tiers(_db(svc), role, champ_id)
        svc.reload()
        return {"changed": changed}

    @app.post("/api/pools/lock")
    def post_lock(request: Request, role: str = Body(...), champ_id: int = Body(...), locked: bool = Body(...)) -> dict:
        svc: Services = request.app.state.services
        if not set_locked(_db(svc), role, champ_id, locked):
            raise HTTPException(404, "no such pool entry")
        svc.reload()
        return {"locked": locked}

    @app.get("/api/learning")
    def get_learning(request: Request) -> dict:
        return request.app.state.services.learning or {"running": False}

    @app.post("/api/learning/run")
    def post_learning(request: Request) -> dict:
        svc: Services = request.app.state.services
        return {"started": svc.learn(), **(svc.learning or {})}

    @app.get("/api/team")
    def get_team(request: Request) -> dict:
        svc: Services = request.app.state.services
        return team_summary(_db(svc), _profiles(svc))

    @app.put("/api/team/style")
    def put_team_style(request: Request, style: str | None = Body(None, embed=True)) -> dict:
        """The style our picks are pulled towards (comp coherence). Applied at once, except mid-draft."""
        svc: Services = request.app.state.services
        db = _db(svc)
        try:
            set_hand_style(db, style or None)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        applied = not svc.in_draft()
        if applied:
            svc.reload()
        return {**team_summary(db, _profiles(svc)), "applied": applied}

    @app.get("/api/prep")
    def get_prep(request: Request) -> dict:
        svc: Services = request.app.state.services
        return {"data_source": svc.data_source, **prep_sheet(svc.engine, svc.catalog.card)}

    @app.get("/api/model-check")
    def get_model_check(request: Request) -> dict:
        return model_check(_db(request.app.state.services))

    # ---------- history ----------
    @app.get("/api/history")
    def get_history(request: Request, queue: str | None = None, side: str | None = None,
                    result: str | None = None, patch: str | None = None) -> dict:
        svc: Services = request.app.state.services
        rows = history_rows(_db(svc), svc.catalog, queue or None, side or None, result or None, patch or None)
        return {"games": rows, "filters": history_filters(history_rows(_db(svc), svc.catalog))}

    @app.get("/api/history/{draft_id}")
    def get_recap(request: Request, draft_id: int) -> dict:
        svc: Services = request.app.state.services
        body = recap(_db(svc), svc.engine, svc.catalog, draft_id)
        if body is None:
            raise HTTPException(404, "no such game")
        report = draft_report(_db(svc), svc.engine, draft_id)
        if report is not None:
            card = svc.catalog.card
            for d in report["deviations"]:
                d["picked"], d["suggested"] = card(d["picked"]), card(d["suggested"])
        body["report"] = report
        return body

    @app.get("/api/art/{version}/{kind}/{key}")
    def get_art(request: Request, version: str, kind: str, key: str):
        """Data Dragon champion art pinned to a patch, fetched once and cached on disk."""
        import re

        from fastapi.responses import FileResponse

        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[A-Za-z0-9]+", key) \
                or kind not in ("square", "loading"):
            raise HTTPException(404)
        svc: Services = request.app.state.services
        cache_dir = svc.settings.cache_dir if svc.settings else PROJECT_ROOT / "cache"
        ext = "png" if kind == "square" else "jpg"
        path = cache_dir / "ddragon" / "art" / version / kind / f"{key}.{ext}"
        if not path.is_file():
            url = (f"{ddragon.BASE_URL}/cdn/{version}/img/champion/{key}.png" if kind == "square"
                   else f"{ddragon.BASE_URL}/cdn/img/champion/loading/{key}_0.jpg")
            try:
                resp = httpx.get(url, timeout=15)
            except httpx.HTTPError:
                raise HTTPException(404) from None
            if resp.status_code != 200:
                raise HTTPException(404)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(resp.content)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})

    if (web_dist / "index.html").is_file():
        app.mount("/", UIFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return ("<h1>Draft Assistant</h1><p>The web UI isn't built yet: run "
                    "<code>npm install &amp;&amp; npm run build</code> in <code>web/</code>.</p>")

    return app


app = create_app()
