"""The learning job, run after every logged game, at startup, and on demand (Pools page):
link players (account-v1), collect their recent games, re-score comfort and tiers.

CLI: `uv run python -m draftassist.learning.jobs`
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import Engine

from draftassist.data.pools import backfill_player_ids, list_players, set_players
from draftassist.data.riot import RiotAuthError, RiotClient
from draftassist.learning.comfort import TierChange, rescore
from draftassist.learning.history import collect_logged, fetch_history

log = logging.getLogger(__name__)
_lock = threading.Lock()  # one learning run at a time


@dataclass
class LearningResult:
    logged_games: int = 0
    history_games: dict[str, int] = field(default_factory=dict)
    proposals: list[TierChange] = field(default_factory=list)
    history_error: str | None = None


def run_learning(db: Engine, make_riot: Callable[[], RiotClient] | None = None) -> LearningResult:
    with _lock:
        result = LearningResult()
        backfill_player_ids(db)
        if make_riot is not None:
            try:
                riot = make_riot()
                unlinked = [p for p in list_players(db) if p["riot_id"] and not p["resolved"]]
                if unlinked:
                    set_players(db, unlinked, riot.account_puuid)
                result.history_games = fetch_history(db, riot)
            except RiotAuthError as e:
                result.history_error = str(e)
            except Exception as e:  # network trouble: still learn from our own logs
                log.exception("match history fetch failed")
                result.history_error = f"match history unavailable ({type(e).__name__})"
        result.logged_games = collect_logged(db)
        result.proposals = rescore(db)
        return result


def run_in_background(db: Engine, make_riot, on_done: Callable[[LearningResult], None] | None = None,
                      on_error: Callable[[], None] | None = None) -> None:
    def work():
        try:
            res = run_learning(db, make_riot)
            log.info("learning: %d new logged games, history %s, %d tier proposals%s", res.logged_games,
                     res.history_games, len(res.proposals), f" ({res.history_error})" if res.history_error else "")
            if on_done is not None:
                on_done(res)
        except Exception:
            log.exception("learning job failed")
            if on_error is not None:
                on_error()

    threading.Thread(target=work, name="learning", daemon=True).start()


def main() -> int:
    from draftassist.config import load_settings
    from draftassist.db.session import init_db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    db = init_db(settings.db_path)
    if settings.has_riot_api_key:
        from draftassist.data.riot import sync_api_key

        sync_api_key(db, settings.riot_api_key)
    make =(lambda: RiotClient(settings.riot_api_key, settings.cache_dir)) if settings.has_riot_api_key else None
    res = run_learning(db, make)
    print(f"Logged games added: {res.logged_games} | match history added per role: {res.history_games}"
          + (f" | {res.history_error}" if res.history_error else ""))
    print(f"Tier proposals: {len(res.proposals)}")
    for p in res.proposals:
        print(f"  {p.role}: champion {p.champ_id} {p.manual_tier} -> {p.scored_tier} ({p.wins}W {p.games - p.wins}L)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
