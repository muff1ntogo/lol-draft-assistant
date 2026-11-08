"""Import missed games into History from match-v5 (docs/import-games.md).

History logs a draft while the app watches champ select. Games played with the app closed (or opened too late) never
reach it. This finds them: each active player's ranked games of the last WINDOW_DAYS, and the matches where at least
MIN_TEAMMATES of them were on the same team. Every such match not in History yet becomes a draft, from what match-v5
gives: both teams' picks and roles, bans, side, result, length, queue and patch.

match-v5 has no pick order: picks fill the format's pick steps in the order the participants are listed (the champ
select order). These drafts are marked `imported`: nothing was suggested, so they carry no suggestion ranks.

CLI: `uv run python -m draftassist.data.import_games` (also `draftassist import-games`, and the Crawl / History pages).
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.crawler import POSITIONS
from draftassist.data.postgame import SIDES, store_own_timeline
from draftassist.data.riot import RiotAuthError
from draftassist.db.models import Draft, DraftAction, Player
from draftassist.engine.formats import get_format
from draftassist.engine.state import SKIPPED

log = logging.getLogger(__name__)

WINDOW_DAYS = 60
MAX_IDS_PER_PLAYER = 300  # 60 days of a busy player's ranked games
MIN_TEAMMATES = 3  # of our players on one team: a game we played together (Flex, Ranked 5s)
MIN_DURATION_S = 300  # remakes aren't imported
STANDARD_QUEUES = {420, 440}  # Solo/Duo, Flex: standard draft; any other ranked queue is Ranked 5s (tournament draft)
SAME_GAME_WINDOW = timedelta(minutes=20)  # a draft logged without a game id, this close to the game's start, is it


@dataclass
class ImportResult:
    linked: int = 0  # our players with an API PUUID
    together: int = 0  # matches with MIN_TEAMMATES of them on one team
    imported: list[int] = field(default_factory=list)  # new draft ids
    already: int = 0  # in History already
    skipped: int = 0  # remakes, missing, unreadable


def game_id_of(match_id: str) -> int | None:
    _, _, n = match_id.partition("_")
    return int(n) if n.isdigit() else None


def _our_players(db: Engine, riot) -> dict[str, Player]:
    """API PUUID -> active player, looking PUUIDs up from Riot IDs where needed (as the learning job does)."""
    with Session(db) as s:
        players = [p for p in s.exec(select(Player).where(Player.active == True)).all()]
        for p in players:
            if not p.api_puuid and p.riot_id:
                try:
                    p.api_puuid = riot.account_puuid(p.riot_id)
                except RiotAuthError:
                    raise
                except Exception:
                    log.exception("could not look up %s", p.riot_id)
                s.add(p)
        s.commit()
        return {p.api_puuid: p for p in s.exec(select(Player).where(Player.active == True)).all()
                if p.api_puuid}


def find_shared_matches(riot, puuids: list[str], since: int) -> dict[str, set[str]]:
    """match id -> which of `puuids` have it in their history (only matches shared by MIN_TEAMMATES or more)."""
    seen: dict[str, set[str]] = defaultdict(set)
    for puuid in puuids:
        ids: list[str] = []
        while len(ids) < MAX_IDS_PER_PLAYER:
            page = riot.match_ids(puuid, None, since, count=100, start=len(ids))
            ids.extend(page)
            if len(page) < 100:
                break
        for match_id in ids:
            seen[match_id].add(puuid)
    return {m: who for m, who in seen.items() if len(who) >= MIN_TEAMMATES}


def _existing(s: Session, game_id: int, start: datetime) -> Draft | None:
    draft = s.exec(select(Draft).where(Draft.game_id == game_id)).first()
    if draft is not None:
        return draft
    for d in s.exec(select(Draft).where(Draft.game_id == None)).all():
        played = d.played_at if d.played_at.tzinfo else d.played_at.replace(tzinfo=UTC)
        if abs(played - start) <= SAME_GAME_WINDOW:
            return d
    return None


def draft_actions(match: dict, our_team: int, fmt_name: str) -> list[tuple[str, str, int, str | None]]:
    """The draft in the format's step order: (side, kind, champ_id, role). Bans by their pick turn, picks in the
    participants' order with their roles (both teams); a skipped or missing ban is SKIPPED. `our_team` is unused
    but kept so callers state whose draft it is."""
    info = match["info"]
    bans: dict[str, list[int]] = {}
    picks: dict[str, list[tuple[int, str | None]]] = {}
    for team in info.get("teams", []):
        side = SIDES.get(team.get("teamId"))
        if side is None:
            continue
        ordered = sorted(team.get("bans") or [], key=lambda b: b.get("pickTurn", 0))
        bans[side] = [b["championId"] if b.get("championId", -1) > 0 else SKIPPED for b in ordered]
    for p in info["participants"]:
        side = SIDES.get(p["teamId"])
        if side is not None:
            picks.setdefault(side, []).append((p["championId"], POSITIONS.get(p.get("teamPosition") or "")))
    out = []
    used = {"ban": defaultdict(int), "pick": defaultdict(int)}
    for step in get_format(fmt_name).steps:
        i = used[step.kind][step.side]
        used[step.kind][step.side] += 1
        if step.kind == "ban":
            champ = bans.get(step.side, [])[i] if i < len(bans.get(step.side, [])) else SKIPPED
            out.append((step.side, "ban", champ, None))
        else:
            champ, role = picks.get(step.side, [])[i] if i < len(picks.get(step.side, [])) else (SKIPPED, None)
            out.append((step.side, "pick", champ, role))  # both sides: match-v5 knows their roles too
    return out


def import_match(db: Engine, match_id: str, match: dict, ours: set[str]) -> int | None:
    """One match into History. None if it was a remake or our team can't be told."""
    info = match["info"]
    if info.get("gameDuration", 0) < MIN_DURATION_S:
        return None
    teams = defaultdict(int)
    for p in info["participants"]:
        if p.get("puuid") in ours:
            teams[p["teamId"]] += 1
    if not teams:
        return None
    our_team = max(teams, key=teams.get)
    queue = info.get("queueId")
    fmt = "ranked" if queue in STANDARD_QUEUES else "tournament"
    version = info.get("gameVersion") or ""
    patch = ".".join(version.split(".")[:2]) or None
    won = next(p["win"] for p in info["participants"] if p["teamId"] == our_team)
    names = {POSITIONS[p["teamPosition"]]: p.get("riotIdGameName") or p.get("summonerName") or ""
             for p in info["participants"] if p["teamId"] == our_team and POSITIONS.get(p.get("teamPosition") or "")}
    with Session(db) as s:
        draft = Draft(game_id=game_id_of(match_id), queue_id=queue, side=SIDES[our_team],
                      played_at=datetime.fromtimestamp(info.get("gameStartTimestamp", 0) / 1000, UTC),
                      result="win" if won else "loss", draft_format=fmt, result_source="match-v5",
                      match_verified=True, game_length_s=int(info["gameDuration"]), patch=patch,
                      ddragon_version=f"{patch}.1" if patch else None,
                      players_json=json.dumps({r: n for r, n in names.items() if n}) or None, imported=True)
        s.add(draft)
        s.flush()
        for seq, (side, kind, champ, role) in enumerate(draft_actions(match, our_team, fmt), start=1):
            s.add(DraftAction(draft_id=draft.draft_id, seq=seq, side=side, type=kind, champ_id=champ, role=role))
        s.commit()
        return draft.draft_id


def import_missed(db: Engine, riot, now: float | None = None, timelines: bool = True) -> ImportResult:
    """Find our shared games of the last WINDOW_DAYS that History doesn't have, and import them (with their timelines,
    for the per-game split)."""
    now = now if now is not None else time.time()
    out = ImportResult()
    players = _our_players(db, riot)
    out.linked = len(players)
    if len(players) < MIN_TEAMMATES:
        return out
    shared = find_shared_matches(riot, list(players), int(now - WINDOW_DAYS * 86_400))
    out.together = len(shared)
    for match_id in sorted(shared):
        game_id = game_id_of(match_id)
        if game_id is None:
            out.skipped += 1
            continue
        try:
            match = riot.match(match_id)
        except RiotAuthError:
            raise
        except Exception:
            log.exception("match %s unreadable", match_id)
            match = None
        if not match:
            out.skipped += 1
            continue
        start = datetime.fromtimestamp(match["info"].get("gameStartTimestamp", 0) / 1000, UTC)
        with Session(db) as s:
            existing = _existing(s, game_id, start)
            if existing is not None:
                if not existing.game_id:  # logged live without a game id: now it has one
                    existing.game_id = game_id
                    s.add(existing)
                    s.commit()
                out.already += 1
                continue
        draft_id = import_match(db, match_id, match, set(players))
        if draft_id is None:
            out.skipped += 1
            continue
        out.imported.append(draft_id)
        if timelines:
            try:
                store_own_timeline(db, riot, draft_id, game_id, match)
            except httpx.HTTPError:
                log.warning("draft %s: timeline not fetched; Confirm games tries again", draft_id)
    if out.imported:
        _after_import(db)
    return out


def _after_import(db: Engine) -> None:
    """The new games' per-game split (docs/draft-vs-execution.md); their win-probability lines fill in on first view."""
    from draftassist.learning import execution

    try:
        execution.refresh_executions(db, only_missing=True)
    except Exception:
        log.exception("splitting the imported games failed")


def main() -> int:
    import sys

    from draftassist.config import load_settings
    from draftassist.data.riot import RiotClient, sync_api_key
    from draftassist.db.session import init_db

    settings = load_settings()
    if not settings.has_riot_api_key:
        print("No Riot API key: add it in Settings first.", file=sys.stderr)
        return 2
    db = init_db(settings.db_path)
    sync_api_key(db, settings.riot_api_key)
    riot = RiotClient(settings.riot_api_key, settings.cache_dir)
    try:
        res = import_missed(db, riot)
    except RiotAuthError as e:
        print(e, file=sys.stderr)
        return 2
    if res.linked < MIN_TEAMMATES:
        print(f"Only {res.linked} of our players are linked to a Riot account; {MIN_TEAMMATES} are needed to tell our "
              "games apart. Set their Riot IDs on the Pools page.")
        return 1
    print(f"Games with {MIN_TEAMMATES}+ of us together in the last {WINDOW_DAYS} days: {res.together}")
    print(f"Imported into History: {len(res.imported)}; already there: {res.already}; skipped (remakes, missing): "
          f"{res.skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
