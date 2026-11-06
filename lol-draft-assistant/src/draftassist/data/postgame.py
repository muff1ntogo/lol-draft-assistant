"""Post-game: confirm each logged draft's result and side against match-v5, and store what its timeline gives.

The client's end-of-game stats give the result straight away; match-v5 is the record of truth and
needs a working Riot key. Custom games are not in match-v5, so they stay unverified. Only derived numbers are kept from
a timeline, never the timeline itself (docs/draft-vs-execution.md, Stage 1).

CLI (verify every unverified draft, then fetch timelines for verified drafts without one):
`uv run python -m draftassist.data.postgame`
"""
from __future__ import annotations

import logging
import threading

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.crawler import POSITIONS
from draftassist.data.riot import PLATFORM, RiotAuthError, RiotClient, sync_api_key
from draftassist.data.timeline_map import parse_timeline
from draftassist.data.timelines import TIMELINE_VERSION, store_team_state
from draftassist.db.models import Draft, DraftAction, OwnGameParticipant, Player

log = logging.getLogger(__name__)

SIDES = {100: "blue", 200: "red"}


def match_id_for(game_id: int) -> str:
    return f"{PLATFORM.upper()}_{game_id}"


def store_own_timeline(db: Engine, riot: RiotClient, draft_id: int, game_id: int, match: dict) -> str:
    """One extra call per game. 'stored', or 'not-found' to be tried again: match-v5 can lag the end of the game."""
    match_id = match_id_for(game_id)
    timeline = riot.timeline(match_id)
    if not timeline:
        return "not-found"
    stats = parse_timeline(timeline, match)  # None: under 15 minutes, so all ten stay at NULL and it's done
    with Session(db) as s:
        for p in match["info"]["participants"]:
            champ = p["championId"]
            values = stats.participants[champ] if stats else {}
            s.merge(OwnGameParticipant(draft_id=draft_id, champ_id=champ, match_id=match_id, team_id=p["teamId"],
                                       role=POSITIONS.get(p.get("teamPosition") or ""), **values))
        if stats:
            store_team_state(s, match_id, stats)
        s.get(Draft, draft_id).timeline_version = TIMELINE_VERSION
        s.commit()
    return "stored"


def fill_own_timelines(db: Engine, riot: RiotClient) -> dict[int, str]:
    """Verified drafts with no timeline yet. Safe to re-run: done ones are skipped, failed ones stay to be tried again."""
    with Session(db) as s:
        drafts = [(d.draft_id, d.game_id) for d in s.exec(select(Draft).where(
            Draft.match_verified == True, Draft.timeline_version < TIMELINE_VERSION)).all() if d.game_id]  # noqa: E712
    results = {}
    for draft_id, game_id in drafts:
        match = riot.match(match_id_for(game_id))
        results[draft_id] = store_own_timeline(db, riot, draft_id, game_id, match) if match else "not-found"
    return results


def verify_draft(db: Engine, riot: RiotClient, draft_id: int) -> str:
    """Returns 'verified', 'corrected', 'not-found' or 'no-game-id'. Also stores the game's timeline: if that fails, the
    verification still stands and `fill_own_timelines` picks it up."""
    outcome = _verify(db, riot, draft_id)
    if outcome in ("verified", "corrected"):
        with Session(db) as s:
            game_id = s.get(Draft, draft_id).game_id
        try:
            store_own_timeline(db, riot, draft_id, game_id, riot.match(match_id_for(game_id)))
        except httpx.HTTPError:
            log.warning("draft %s: timeline not fetched; it stays to be tried again", draft_id)
    return outcome


def _verify(db: Engine, riot: RiotClient, draft_id: int) -> str:
    with Session(db) as s:
        draft = s.get(Draft, draft_id)
        if draft is None or not draft.game_id:
            return "no-game-id"
        match = riot.match(match_id_for(draft.game_id))
        if not match:
            return "not-found"
        our_puuids = {p.api_puuid for p in s.exec(select(Player)).all() if p.api_puuid}
        our_picks = {a.champ_id for a in s.exec(select(DraftAction).where(
            DraftAction.draft_id == draft_id, DraftAction.type == "pick", DraftAction.side == draft.side)).all()}
        participants = match["info"]["participants"]
        team = next((p["teamId"] for p in participants if p.get("puuid") in our_puuids), None)
        if team is None:  # players not resolved yet: our team is the one with our picks
            counts = {t: sum(1 for p in participants if p["teamId"] == t and p["championId"] in our_picks)
                      for t in SIDES}
            team = max(counts, key=counts.get) if any(counts.values()) else None
        if team is None:
            return "not-found"
        won = next(p["win"] for p in participants if p["teamId"] == team)
        result, side = ("win" if won else "loss"), SIDES[team]
        corrected = (draft.result not in (None, result)) or draft.side != side
        if corrected:
            log.warning("draft %s corrected by match-v5: %s/%s -> %s/%s", draft_id, draft.side, draft.result, side, result)
        draft.result, draft.side, draft.match_verified, draft.result_source = result, side, True, "match-v5"
        if match["info"].get("queueId") is not None:
            draft.queue_id = match["info"]["queueId"]
        s.add(draft)
        s.commit()
        return "corrected" if corrected else "verified"


def verify_unverified(db: Engine, riot: RiotClient) -> dict[int, str]:
    with Session(db) as s:
        ids = [d.draft_id for d in s.exec(select(Draft).where(Draft.match_verified == False)).all()]
    return {i: verify_draft(db, riot, i) for i in ids}


def _attribute_new_games(db: Engine) -> None:
    """The game's timeline has only just arrived, after the learning job ran for it."""
    from draftassist.learning import execution

    try:
        execution.refresh_executions(db, only_missing=True)
    except Exception:
        log.exception("attributing games failed")


def schedule_verification(db: Engine, make_riot, draft_id: int, delays=(120.0, 600.0)) -> None:
    """match-v5 lags the end of the game: try after each delay until it is found. Background thread."""

    def attempt(remaining: tuple[float, ...]) -> None:
        try:
            riot = make_riot()
            outcome = verify_draft(db, riot, draft_id)
        except RiotAuthError as e:
            log.warning("draft %s not verified: %s", draft_id, e)
            return
        except Exception:
            log.exception("draft %s verification failed", draft_id)
            outcome = "not-found"
        if outcome == "not-found" and remaining:
            threading.Timer(remaining[0], attempt, args=(remaining[1:],)).start()
        elif outcome in ("verified", "corrected"):
            _attribute_new_games(db)

    threading.Timer(delays[0], attempt, args=(tuple(delays[1:]),)).start()


def main() -> int:
    import sys

    from draftassist.config import load_settings
    from draftassist.db.session import init_db

    settings = load_settings()
    db = init_db(settings.db_path)
    try:
        if settings.has_riot_api_key:
            sync_api_key(db, settings.riot_api_key)
        riot = RiotClient(settings.riot_api_key, settings.cache_dir)
        results = verify_unverified(db, riot)
        timelines = fill_own_timelines(db, riot)
    except RiotAuthError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"Checked {len(results)} drafts: " + (", ".join(f"#{k} {v}" for k, v in results.items()) or "nothing to do"))
    print(f"Timelines for {len(timelines)} drafts: " + (", ".join(f"#{k} {v}" for k, v in timelines.items()) or "nothing to do"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
