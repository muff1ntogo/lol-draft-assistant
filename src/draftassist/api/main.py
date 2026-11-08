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
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from draftassist.api.scout_routes import router as scout_router
from draftassist.api.jobs_routes import router as jobs_router
from draftassist.api.setup_routes import router as setup_router
from draftassist.api.account_routes import router as account_router
from draftassist.api.datasets_routes import router as datasets_router
from draftassist.api.storage_routes import router as storage_router
from draftassist.api.shared_routes import router as shared_router
from draftassist.api.synergy_routes import router as synergy_router
from draftassist.config import DATA_ROOT, RESOURCE_ROOT, Settings, ensure_user_files, load_settings
from draftassist.data import ddragon
from draftassist.data.pools import (
    PlayerDirectoryDb,
)
from draftassist.data.profiles import load_profiles
from draftassist.data.provider import (
    ChampionCatalog,
    load_catalog,
    load_engine_inputs,
    stats_version,
)
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
WEB_DIST = RESOURCE_ROOT / "web" / "dist"
RECORDINGS = DATA_ROOT / "fixtures" / "lcu" / "recorded"  # real names and PUUIDs: never committed (CLAUDE.md)
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

    scout_manual: list = field(default_factory=list)  # opponents' Riot IDs pasted on Live, for the current game
    scout_asked: set = field(default_factory=set)  # Riot IDs (lower case) already asked for in this game
    scout_retried: set = field(default_factory=set)  # ... and asked for again after their fetch failed (once a game)

    def __post_init__(self) -> None:
        if self.db is not None:
            self.live.scout_provider = self.scout_info
            self.live.on_game_end = self.end_scouting
        if self.db is not None and self.pinned is None:
            self.pinned = load_pinned(self.db)
        self.live.pin_provider = lambda: self.pinned  # read on every champ select event
        if self.live.logger is not None:
            self.live.logger.pin_provider = lambda: self.pinned
    _synergy_v2: object | None = None  # built lazily, dropped on reload and after tag / rule edits
    _synergy_pending: object | None = None  # a rebuilt evaluator waiting for the draft to end
    roster_path: Path | None = None  # data/roster.json (docs/player-roster.md); None: not mirrored (tests)
    shared_store: object | None = None  # tests: a FakeSharedStore; else from the signed-in account
    account_store: object | None = None  # data/shared/account.AccountStore; tests point it at a scratch folder
    members_override: object | None = None  # tests: a data/shared/members.Members on fakes
    selection_pending: bool = False  # a data selection saved during a draft, applied after it (Stage 4)
    shared_last: dict | None = None  # the last pull's outcome, for the Crawl page

    def save_roster(self) -> None:
        """Mirror our players and pools to the roster file, after any change to them."""
        if self.roster_path is None or self.db is None:
            return
        from draftassist.data.roster import save

        champs = self.catalog.champs
        try:
            save(self.db, self.roster_path, lambda c: champs.get(c, ("", str(c)))[1])
        except Exception:
            log.exception("could not save the roster to %s", self.roster_path)

    scout_runner_override: object | None = None  # tests: a ScoutRunner with a fake Riot client
    _scout_runner: object | None = None

    def scouting(self):
        """Enemy scouting in the background (docs/enemy-scouting.md). Fetches nothing without a Riot API key."""
        if self.scout_runner_override is not None:
            return self.scout_runner_override
        if self._scout_runner is None:
            from draftassist.data.scout import ScoutRunner

            self._scout_runner = ScoutRunner(self.db, self.make_riot(), on_progress=self.live.refresh)
        return self._scout_runner

    def opponents(self, live=None) -> tuple[list[str], dict[str, str | None], str | None]:
        """Who we face: the client's Riot IDs when it gives them (with their positions), else the ones pasted on Live.
        When the client names only some of them, pasted IDs it doesn't name fill the rest (Stage E)."""
        current = live if live is not None else self.live.current
        if current is not None:
            given = {s.riot_id: s.role for s in current.their_slots if s.riot_id}
            if given:
                known = {rid.lower() for rid in given}
                for rid in self.scout_manual:
                    if len(given) >= 5:
                        break
                    if rid.lower() not in known:
                        given[rid] = None
                        known.add(rid.lower())
                return list(given), given, "client"
        if self.scout_manual:
            return list(self.scout_manual), dict.fromkeys(self.scout_manual), "manual"
        return [], {}, None

    def current_patch(self) -> str | None:
        """Riot's major.minor of the champion data in use, for weighting scouted games by patch."""
        patch = self.catalog.patch if self.catalog is not None else None
        return ".".join(patch.split(".")[:2]) if patch else None

    def scout_info(self, live=None):
        """(EnemyScouting | None, what Live shows): asks for any opponent not fetched yet. Off in Settings: nothing."""
        from draftassist.data.scout import (
            ERROR_RETRY_S,
            build_scouting,
            guess_roles,
            load_pools,
            scout_summary,
        )
        from draftassist.data.settings_store import get_engine_settings

        if self.lcu_mode == "demo":  # the demo's enemy names are made up: they could be someone real's
            return None, None
        if live is not None and live.state.format.name != "tournament":
            return None, None  # Ranked 5s only (decision 8 Nov 2026): Flex and Solo/Duo don't show who we face
        ids, given, source = self.opponents(live)
        settings = get_engine_settings(self.db)
        if not ids or not settings["scout_enabled"]:
            return None, None
        runner = self.scouting()
        if live is not None and runner.available:  # a running crawl pauses so scouting gets the key's rate limit
            try:
                self.jobs().make_way()
            except Exception:
                log.exception("could not ask the crawl to make way")
        new = [rid for rid in ids if rid.lower() not in self.scout_asked]
        if new and runner.available:  # once per player per game: a refresh never sets off another fetch
            self.scout_asked.update(rid.lower() for rid in new)
            runner.enqueue(new)
        pools = load_pools(self.db, ids, current_patch=self.current_patch())
        if runner.available and runner.last_error is None:  # a fetch that failed (not a bad key): once more, later
            retry = [rid for rid, p in pools.items() if p.status == "error" and rid.lower() not in self.scout_retried
                     and time.time() - p.fetched_at >= ERROR_RETRY_S]
            if retry:
                self.scout_retried.update(rid.lower() for rid in retry)
                runner.enqueue(retry)
        shares = self.engine.role_share_map
        roles, sure = guess_roles(pools, given, shares)
        scouting = build_scouting(pools, given, settings["scout_weight"], shares)
        info = {"source": source, "available": runner.available, "running": runner.running, "error": runner.last_error,
                "players": scout_summary(ids, pools, roles, self.catalog.card, sure=sure)}
        return scouting, info

    # ---------- first run and updates (src/draftassist/first_run.py, updates.py) ----------
    updater_override: object | None = None  # tests: an Updater with fakes
    _updater: object | None = None
    bootstrap: dict = field(default_factory=lambda: {"state": "idle", "jobs": {}})

    def updater(self):
        if self.updater_override is not None:
            return self.updater_override
        if self._updater is None:
            from draftassist.updates import Updater

            self._updater = Updater()
        return self._updater

    job_runner_override: object | None = None  # tests: a JobRunner with a fake process
    _job_runner: object | None = None

    def jobs(self):
        """The Crawl page's jobs, one at a time, each its own process (jobs.py). A finished job reloads the stats."""
        if self.job_runner_override is not None:
            return self.job_runner_override
        if self._job_runner is None:
            from draftassist.config import DATA_ROOT
            from draftassist.jobs import JobRunner

            def done(job: str) -> None:
                self.refresh_stats()

            self._job_runner = JobRunner(DATA_ROOT / "cache" / "jobs",
                                         has_key=lambda: bool(self.settings and self.settings.has_riot_api_key),
                                         on_done=done)
        return self._job_runner

    def first_pull_needed(self) -> bool:
        """A teammate's install that has never pulled (docs/shared-data.md, Stage 5)."""
        from draftassist.data.shared.pull import pull_status

        return (self.db is not None and self.account().role == "user" and not self.crawls_here()
                and pull_status(self.db)["publish_id"] is None)

    def start_bootstrap(self, run_job=None, wait: bool = False) -> bool:
        """A fresh install, in the background, then reload: a teammate's first pull of the shared stats (with progress
        for the banner), then the champions and, unless shared stats arrived, OP.GG's starting stats (only the fallback
        for users). False if there's nothing to do or it's already running. `wait`: run here (tests)."""
        from draftassist.first_run import BOOTSTRAP_JOBS, bootstrap, needs_bootstrap, pull_shared_first_run

        first_pull = self.first_pull_needed()
        if self.db is None or self.bootstrap["state"] == "running" or not (first_pull or needs_bootstrap(self.db)):
            return False
        self.bootstrap = {"state": "running", "jobs": {}}
        if first_pull:  # the banner's progress bar
            self.bootstrap["pull"] = {"done": 0, "total": 0}

        def work() -> None:
            jobs: dict[str, str] = {}
            pulled = False
            if first_pull:
                def progress(done: int, total: int) -> None:
                    self.bootstrap = {**self.bootstrap, "pull": {"done": done, "total": total}}

                status = pull_shared_first_run(self.db, self._shared(), log.info, progress)
                self.shared_last = {"state": status.state, "message": status.message, "publish_id": status.publish_id,
                                    "blocks": status.blocks, "at": time.time()}
                jobs["pull"] = "done" if status.state in ("updated", "unchanged") else status.state
                pulled = status.state == "updated"
            if needs_bootstrap(self.db):
                wanted = tuple(j for j in BOOTSTRAP_JOBS if not (pulled and j == "opgg"))  # OP.GG: the fallback only
                jobs.update(bootstrap(run_job, wanted))
            done = {"state": "done", "jobs": jobs}
            if first_pull:
                done["pull"] = self.bootstrap.get("pull")
            self.bootstrap = done
            try:
                self.refresh_stats()  # the new champions and stats change the stats version: the engine reloads
            except Exception:
                log.exception("reload after the first-run setup failed")

        import threading

        if wait:
            work()
        else:
            threading.Thread(target=work, name="first-run", daemon=True).start()
        return True

    def set_riot_key(self, key: str) -> None:
        """Save the key in the data folder's .env and use it from now on (the scouting runner picks it up)."""
        from dataclasses import replace

        from draftassist.config import DATA_ROOT
        from draftassist.first_run import save_riot_key

        save_riot_key(key, DATA_ROOT / ".env")
        self.settings = replace(self.settings or Settings(), riot_api_key=key.strip())
        self._scout_runner = None

    def end_scouting(self) -> None:
        """The game is over: forget the opponents pasted for it, and who was asked for (the next game asks again)."""
        self.scout_manual.clear()
        self.scout_asked.clear()
        self.scout_retried.clear()

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
        live.draft_scale = load_draft_scale(self.db)
        opgg = self.live.strategy_builds.opgg if getattr(self.live.strategy_builds, "opgg", None) else None
        live.strategy_builds = strategy_builds(self.db, self.settings.cache_dir if self.settings else None, inputs, opgg)
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

    # ---------- shared data (docs/shared-data.md, Stages 2-3) ----------
    def account(self):
        """The signed-in account: admin, user or solo (data/shared/account.py)."""
        if self.account_store is None:
            from draftassist.data.shared.account import default_store

            self.account_store = default_store()
        return self.account_store.load()

    def members(self):
        if self.members_override is not None:
            return self.members_override
        from draftassist.data.shared.members import members_for

        self.account()
        return members_for(self.account_store)

    def _shared(self):
        if self.shared_store is not None:
            return self.shared_store
        from draftassist.data.shared.config import open_store

        self.account()
        config = self.account_store.shared_config()
        return open_store(config) if config is not None else None

    def crawls_here(self) -> bool:
        """This machine has its own crawl (the admin, or solo mode): it publishes, it doesn't pull over its own data."""
        from sqlalchemy import text

        with self.db.connect() as conn:
            return conn.execute(text("SELECT EXISTS (SELECT 1 FROM crawled_matches)")).scalar() == 1

    def sync_shared(self) -> dict:
        """Pull newer shared data, then reload the stats (between drafts, as every stats refresh). Never in a draft; on any
        failure the last pull stays."""
        from draftassist.data.shared.pull import pull

        store = self._shared() if self.db is not None else None
        if store is None:
            self.shared_last = {"state": "off", "message": "No shared database set up."}
        elif self.crawls_here():
            self.shared_last = {"state": "local", "message": "This machine crawls its own data: nothing to pull."}
        else:
            try:
                status = pull(self.db, store, in_draft=self.in_draft, log=log.info)
                self.shared_last = {"state": status.state, "message": status.message, "publish_id": status.publish_id,
                                    "blocks": status.blocks, "at": time.time()}
            except Exception as e:
                log.exception("shared pull failed; keeping the last pull")
                self.shared_last = {"state": "error", "message": str(e), "at": time.time()}
            if self.shared_last["state"] == "updated":
                self.refresh_stats()
        return self.shared_last

    def shared_status(self) -> dict:
        from draftassist.data.shared.pull import pull_status

        account = self.account()
        configured = self.shared_store is not None or account.role != "solo"
        return {"configured": configured, "account": account.as_dict(),
                "pull": pull_status(self.db) if self.db is not None else None, "last": self.shared_last}

    def in_draft(self) -> bool:
        """True while a champ select is running (suggestions must not change under the shot-caller)."""
        latest = self.hub.latest
        return self.live.gameflow_phase == "ChampSelect" or (
            latest.get("status") == "champ_select" and latest.get("phase") != "Locked in")

    def refresh_stats(self) -> str:
        """Reload stats if the crawl (or anything else) changed them, but only between drafts.
        Returns 'unchanged', 'deferred' (in a draft; retried next time) or 'reloaded'."""
        if self.selection_pending and self.db is not None and not self.in_draft():
            from draftassist.data.datasets import apply_selection

            apply_selection(self.db)
            self.selection_pending = False
        if self._synergy_pending is not None and not self.in_draft():
            self._swap_synergy(self._synergy_pending)
        if self.db is not None and not self.in_draft():
            self.live.draft_scale = load_draft_scale(self.db)  # a new execution fit doesn't change the stats version
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
    ensure_user_files()  # first run of the installed app: the tags and rules YAML into the data folder
    db = init_db(settings.db_path)
    from draftassist.data.roster import restore, roster_file

    roster_path = roster_file()
    try:  # a database without our players (new install, reset): the roster comes back from its file
        if restore(db, roster_path):
            from draftassist.learning.comfort import rescore

            rescore(db)
    except Exception:
        log.exception("could not restore the roster from %s", roster_path)
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
    live.draft_scale = load_draft_scale(db)
    opgg = opgg_builds(settings.cache_dir, live) if settings.lcu_mode != "off" else None  # tests run with "off"
    live.strategy_builds = strategy_builds(db, settings.cache_dir, inputs, opgg)
    if logger is not None:
        logger.context = lambda: (live.engine, live.catalog)
    svc = Services(hub, live, db, players, settings.lcu_mode, settings, stats_version(db))
    svc.roster_path = roster_path
    svc.save_roster()  # the file exists from the first start, and follows every change after
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


def strategy_builds(db: Engine, cache_dir: Path | None, inputs, opgg=None):
    """The Strategy view's build reader (docs/strategy.md) for the current patch and our players' brackets, with OP.GG's
    builds and matchup guides when `opgg` is given (docs/top-lane-power.md, Stage 1)."""
    from draftassist.api.strategy import StrategyBuilds
    from draftassist.data.crawler import current_patch

    try:
        patch = current_patch(db)
    except RuntimeError:  # no champions yet
        return None
    names = {c: name for c, (_, name) in inputs.catalog.champs.items()}
    return StrategyBuilds(db, cache_dir or Path("cache"), patch, dict(inputs.team.brackets), inputs.catalog.patch,
                          names, opgg)


def opgg_builds(cache_dir: Path, live) -> object | None:
    """OP.GG builds fetched in the background for the Strategy tab; the Live screen refreshes when one arrives."""
    from draftassist.data.opgg_builds import OpggBuilds

    return OpggBuilds(cache_dir, on_ready=live.refresh)


def load_draft_scale(db: Engine) -> float | None:
    """The draft log-odds scale from the latest execution fit (docs/draft-vs-execution.md, decision 17), or None before the
    first fit. Only that one number, so Live doesn't load the whole model."""
    import json

    from sqlmodel import select

    from draftassist.db.models import ExecutionFit

    with Session(db) as s:
        rows = s.exec(select(ExecutionFit)).all()
    if not rows:
        return None
    from draftassist.data.synergy import latest_window, pick_fit

    latest = pick_fit(rows, latest_window(db))
    scale = json.loads(latest.body).get("draft_scale")
    return float(scale) if scale is not None else None


async def check_updates_forever(svc: Services) -> None:
    """Look for a newer release now and every few hours (updates.py); the Update button reads the result."""
    from draftassist.updates import CHECK_EVERY_S

    while True:
        try:
            await asyncio.to_thread(svc.updater().check)
        except Exception:
            log.exception("update check failed")
        await asyncio.sleep(CHECK_EVERY_S)


SHARED_PULL_EVERY_S = 3 * 3600


async def sync_shared_forever(svc: Services, every_s: float = SHARED_PULL_EVERY_S, first_delay_s: float = 0) -> None:
    """Pull the shared data at start and every few hours (Services.sync_shared skips drafts and crawling machines).
    `first_delay_s`: wait first, when the first-run setup is doing the first pull."""
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            await asyncio.to_thread(svc.sync_shared)
        except Exception:
            log.exception("shared pull failed")
        await asyncio.sleep(every_s)


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
        LcuConnector(live.submit, live.handle_phase, live.handle_status, live.handle_end_of_game).start()
    elif svc.lcu_mode == "demo":
        return asyncio.create_task(run_demo(svc.live))
    elif svc.lcu_mode == "replay" and svc.settings is not None:
        folder = Path(svc.settings.replay_dir or RESOURCE_ROOT / "fixtures" / "lcu" / "flex-ranked-sample")
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
        update_task = shared_task = None
        if services is None:  # the real app, not a test: set itself up, and look for updates and shared data
            # a teammate's first pull runs inside the setup, with a progress banner, so the window opens at once
            first_pull = svc.start_bootstrap() and svc.bootstrap.get("pull") is not None
            update_task = asyncio.create_task(check_updates_forever(svc))
            shared_task = asyncio.create_task(sync_shared_forever(
                svc, first_delay_s=SHARED_PULL_EVERY_S if first_pull else 0))
        yield
        for task in (demo_task, refresh_task, update_task, shared_task):
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

    for router in (pool_router, settings_router, synergy_router, learning_router, history_router, scout_router,
                   setup_router, jobs_router, shared_router, account_router, datasets_router, storage_router):
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
