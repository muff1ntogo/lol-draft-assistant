"""Enemy scouting, Stage A: what the players we face actually play (docs/enemy-scouting.md).

Fast first: account-v1 and champion-mastery-v4 for every player (two requests each), so a pool exists before the bans.
Then their ranked games of the last WINDOW_DAYS from match-v5, up to MAX_GAMES each, read one match per player in turn so
all five fill in evenly. Every response is cached by RiotClient, so a team met again costs nothing.

CLI: `draftassist scout-check [folder]` reports which identity fields the enemy team carries in recorded champ selects,
per queue (field names and counts only, never values).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, func
from sqlmodel import Session, select

from draftassist.data.crawler import POSITIONS
from draftassist.data.riot import RiotAuthError
from draftassist.db.models import ScoutGame, ScoutPlayer
from draftassist.engine.model import ROLES
from draftassist.engine.scouting import SCOUT_WEIGHT, EnemyScouting, ScoutedPlayer

log = logging.getLogger(__name__)

WINDOW_DAYS = 60
MAX_GAMES = 20  # per player: about 2 minutes for a whole team at the key's 100 requests / 2 minutes
MASTERY_COUNT = 20
REFRESH_S = 6 * 3600  # a player scouted this recently isn't fetched again
MIN_DURATION_S = 300  # remakes don't count
TEAM_SIZE = 5

# "Name#TAG": game names are 3-16 characters and may hold spaces; tags 2-5 letters or digits
_RIOT_ID = re.compile(r"([^\s,#;|][^,#;|\n]{1,15}?)\s*#\s*([0-9A-Za-zÀ-￿]{2,5})(?![0-9A-Za-z])")
_LOBBY_NOISE = re.compile(r"\s*(joined the (lobby|room)|left the (lobby|room))\s*$", re.IGNORECASE)


def parse_riot_ids(text: str) -> list[str]:
    """Riot IDs from pasted text: one per line, comma-separated, or an OP.GG multi-search / lobby chat paste
    ("Name #TAG joined the lobby"). Unique, in order, at most a team's worth."""
    out: list[str] = []
    seen: set[str] = set()
    for line in re.split(r"[\n,;|]", text):
        line = _LOBBY_NOISE.sub("", line)
        for name, tag in _RIOT_ID.findall(line):
            rid = f"{name.strip()}#{tag}"
            if rid.lower() not in seen:
                seen.add(rid.lower())
                out.append(rid)
    return out[:TEAM_SIZE]


# ---------- reading a player's pool ----------
@dataclass(frozen=True)
class PlayerPool:
    """A scouted player: games and wins per role and champion, and mastery points."""
    riot_id: str
    status: str
    games: dict[str, dict[int, tuple[int, int]]] = field(default_factory=dict)  # role -> champ -> (games, wins)
    mastery: tuple[tuple[int, int], ...] = ()  # (champ, points), highest first
    games_wanted: int = 0

    @property
    def games_read(self) -> int:
        return sum(g for champs in self.games.values() for g, _ in champs.values())

    @property
    def main_role(self) -> str | None:
        totals = {r: sum(g for g, _ in champs.values()) for r, champs in self.games.items()}
        return max(totals, key=lambda r: (totals[r], r)) if totals else None

    def as_dict(self) -> dict:
        return {"riot_id": self.riot_id, "status": self.status, "games_read": self.games_read,
                "games_wanted": self.games_wanted, "main_role": self.main_role,
                "games": {r: {str(c): list(gw) for c, gw in champs.items()} for r, champs in self.games.items()},
                "mastery": [list(m) for m in self.mastery]}


def _find(s: Session, riot_id: str) -> ScoutPlayer | None:
    return s.exec(select(ScoutPlayer).where(func.lower(ScoutPlayer.riot_id) == riot_id.lower())).first()


def load_pools(db: Engine, riot_ids: Iterable[str], now: float | None = None) -> dict[str, PlayerPool]:
    """Riot ID (as asked) -> what's known, for those scouted at all. Games older than the window don't count."""
    since_ms = int(((now if now is not None else time.time()) - WINDOW_DAYS * 86_400) * 1000)
    out = {}
    with Session(db) as s:
        for rid in riot_ids:
            p = _find(s, rid)
            if p is None:
                continue
            games: dict[str, dict[int, tuple[int, int]]] = {}
            if p.api_puuid:
                for g in s.exec(select(ScoutGame).where(ScoutGame.api_puuid == p.api_puuid,
                                                        ScoutGame.game_start_ms >= since_ms)).all():
                    if g.role:
                        n, w = games.setdefault(g.role, {}).get(g.champ_id, (0, 0))
                        games[g.role][g.champ_id] = (n + 1, w + int(g.win))
            mastery = tuple((int(c), int(pts)) for c, pts in json.loads(p.mastery_json or "[]"))
            out[rid] = PlayerPool(p.riot_id, p.status, games, mastery, p.games_wanted)
    return out


def assign_roles(pools: Mapping[str, PlayerPool], given: Mapping[str, str | None]) -> dict[str, str]:
    """Riot ID -> the role we take them to play: the client's position when it gives one, else their most played free
    role (players with the most games choose first). A player with no games and no position gets none."""
    roles = {rid: r for rid, r in given.items() if r in ROLES}
    taken = set(roles.values())
    rest = sorted((rid for rid in pools if rid not in roles), key=lambda rid: -pools[rid].games_read)
    for rid in rest:
        by_games = sorted(pools[rid].games.items(), key=lambda kv: -sum(g for g, _ in kv[1].values()))
        role = next((r for r, _ in by_games if r not in taken), None)
        if role is not None:
            roles[rid] = role
            taken.add(role)
    return roles


def build_scouting(pools: Mapping[str, PlayerPool], given_roles: Mapping[str, str | None],
                   weight: float = SCOUT_WEIGHT) -> EnemyScouting | None:
    """What the engine reads (engine/scouting.py), or None if nobody has anything to go on yet."""
    roles = assign_roles(pools, given_roles)
    players = []
    for rid, role in roles.items():
        pool = pools.get(rid)
        if pool is None or (not pool.games and not pool.mastery):
            continue
        any_role: dict[int, int] = {}
        for champs in pool.games.values():
            for c, (g, _) in champs.items():
                any_role[c] = any_role.get(c, 0) + g
        games = tuple(sorted(((c, g, w) for c, (g, w) in pool.games.get(role, {}).items()), key=lambda t: (-t[1], t[0])))
        players.append(ScoutedPlayer(rid.split("#")[0], role, games, tuple(sorted(any_role.items())), pool.mastery))
    return EnemyScouting(tuple(players), weight) if players else None


TOP_SHOWN = 3


def scout_summary(riot_ids: Iterable[str], pools: Mapping[str, PlayerPool], roles: Mapping[str, str],
                  card: Callable[[int], dict | None]) -> list[dict]:
    """Per player, what Live shows: their name, the role we take them to play, how far the fetch is, and their top
    champions in that role (games and win rate), or by mastery until games are in."""
    out = []
    for rid in riot_ids:
        pool, role = pools.get(rid), roles.get(rid)
        top: list[dict] = []
        if pool is not None:
            played = pool.games.get(role, {}) if role else {}
            if played:
                for c, (g, w) in sorted(played.items(), key=lambda kv: (-kv[1][0], kv[0]))[:TOP_SHOWN]:
                    top.append({"champ": card(c), "games": g, "wins": w})
            else:
                top = [{"champ": card(c), "points": p} for c, p in pool.mastery[:TOP_SHOWN]]
        out.append({"riot_id": rid, "name": rid.split("#")[0], "role": role, "status": pool.status if pool else "new",
                    "games_read": pool.games_read if pool else 0, "games_wanted": pool.games_wanted if pool else 0,
                    "top": top})
    return out


# ---------- fetching ----------
def _save(db: Engine, riot_id: str, **values) -> None:
    with Session(db) as s:
        p = _find(s, riot_id) or ScoutPlayer(riot_id=riot_id)
        for k, v in values.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()


def scout(db: Engine, riot, riot_ids: Iterable[str], now: float | None = None, force: bool = False,
          progress: Callable[[], None] | None = None) -> dict[str, str]:
    """Fetch the players' mastery, then their recent ranked games, interleaved. Returns each Riot ID's final status.
    A player scouted within REFRESH_S is skipped unless forced. A bad key stops everything (RiotAuthError); any other
    trouble with one player marks that player "error" and goes on. `progress` is called once mastery is in and after
    each round of matches, so Live can update as the pools fill."""
    tell = progress or (lambda: None)
    now = now if now is not None else time.time()
    since = int(now - WINDOW_DAYS * 86_400)
    ids = list(dict.fromkeys(riot_ids))
    statuses: dict[str, str] = {}
    match_ids: dict[str, tuple[str, list[str]]] = {}  # riot id -> (puuid, its match ids still to read)

    for rid in ids:  # 1. who they are and what they've mastered: before the bans
        with Session(db) as s:
            known = _find(s, rid)
            fresh = known is not None and known.status in ("done", "missing") and now - known.fetched_at < REFRESH_S
        if fresh and not force:
            statuses[rid] = known.status
            continue
        try:
            puuid = riot.account_puuid(rid)
            if not puuid:
                _save(db, rid, api_puuid=None, status="missing", fetched_at=now)
                statuses[rid] = "missing"
                continue
            mastery = [[m["championId"], m["championPoints"]] for m in riot.mastery_top(puuid, MASTERY_COUNT)]
            _save(db, rid, api_puuid=puuid, mastery_json=json.dumps(mastery), status="mastery", fetched_at=now)
            statuses[rid] = "mastery"
        except RiotAuthError:
            raise
        except Exception:
            log.exception("scouting %s failed", rid)
            _save(db, rid, status="error", fetched_at=now)
            statuses[rid] = "error"

    tell()
    for rid, status in list(statuses.items()):  # 2. which games to read
        if status != "mastery":
            continue
        try:
            with Session(db) as s:
                puuid = _find(s, rid).api_puuid
            found = riot.match_ids(puuid, None, since, count=MAX_GAMES)[:MAX_GAMES]
            _save(db, rid, status="games", games_wanted=len(found))
            match_ids[rid] = (puuid, list(found))
            statuses[rid] = "games"
        except RiotAuthError:
            raise
        except Exception:
            log.exception("scouting %s's match ids failed", rid)
            _save(db, rid, status="error")
            statuses[rid] = "error"

    for i in range(MAX_GAMES):  # 3. one match per player in turn, so all five fill in evenly
        read = False
        for rid, (puuid, found) in match_ids.items():
            if i >= len(found) or statuses[rid] == "error":
                continue
            try:
                read = _read_match(db, riot, puuid, found[i]) or read
            except RiotAuthError:
                raise
            except Exception:
                log.exception("scouting %s's match %s failed", rid, found[i])
        if read:
            tell()
    for rid in match_ids:
        if statuses[rid] == "games":
            _save(db, rid, status="done", fetched_at=now)
            statuses[rid] = "done"
    tell()
    return statuses


def _read_match(db: Engine, riot, puuid: str, match_id: str) -> bool:
    """Store one of their games; True if a new one was stored."""
    with Session(db) as s:
        if s.get(ScoutGame, (puuid, match_id)) is not None:
            return False
    match = riot.match(match_id)
    if not match:
        return False
    info = match["info"]
    if info.get("gameDuration", 0) < MIN_DURATION_S:
        return False
    me = next((x for x in info["participants"] if x.get("puuid") == puuid), None)
    if me is None:
        return False
    with Session(db) as s:
        s.add(ScoutGame(api_puuid=puuid, match_id=match_id, champ_id=me["championId"],
                        role=POSITIONS.get(me.get("teamPosition") or ""), win=bool(me["win"]),
                        queue_id=info.get("queueId"), game_start_ms=info.get("gameStartTimestamp", 0)))
        s.commit()
    return True


class ScoutRunner:
    """Scouting in the background, one batch at a time; Riot IDs asked for meanwhile wait for the next batch."""

    def __init__(self, db: Engine, make_riot: Callable[[], object] | None,
                 on_progress: Callable[[], None] | None = None):
        self.db, self.make_riot, self.on_progress = db, make_riot, on_progress
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._force = False
        self._running = False
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return self.make_riot is not None

    @property
    def running(self) -> bool:
        return self._running

    def enqueue(self, riot_ids: Iterable[str], force: bool = False) -> bool:
        """Queue Riot IDs; starts the worker if it isn't running. False without a Riot API key."""
        if self.make_riot is None:
            return False
        with self._lock:
            for rid in riot_ids:
                if rid.lower() not in {p.lower() for p in self._pending}:
                    self._pending.append(rid)
            self._force = self._force or force
            if self._running:
                return True
            self._running = True
        threading.Thread(target=self._work, name="scout", daemon=True).start()
        return True

    def _work(self) -> None:
        try:
            while True:
                with self._lock:
                    batch, self._pending = self._pending, []
                    force, self._force = self._force, False
                    if not batch:
                        self._running = False
                        return
                riot = self.make_riot()
                try:
                    scout(self.db, riot, batch, force=force, progress=self.on_progress)
                    self.last_error = None
                except RiotAuthError as e:
                    self.last_error = str(e)
                except Exception:
                    log.exception("scouting failed")
                    self.last_error = "scouting failed (see log)"
                finally:
                    close = getattr(riot, "close", None)
                    if close:
                        close()
                if self.on_progress:
                    self.on_progress()
        finally:
            with self._lock:
                self._running = False


# ---------- what the client gives: a check on recorded champ selects ----------
def identity_report(folder: Path) -> dict[int | None, dict[str, int]]:
    """queue id -> theirTeam field -> how many recorded sessions had it set for at least one enemy. Names only."""
    out: dict[int | None, Counter] = {}
    sessions: Counter = Counter()
    for f in sorted(Path(folder).rglob("*-session.json")):
        try:
            session = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(session, dict) or not isinstance(session.get("theirTeam"), list):
            continue
        queue = session.get("queueId")
        sessions[queue] += 1
        seen = {k for m in session["theirTeam"] if isinstance(m, dict) for k, v in m.items() if v not in ("", 0, None, -1)}
        out.setdefault(queue, Counter()).update(seen)
    return {q: {"sessions": sessions[q], **dict(sorted(c.items()))} for q, c in out.items()}


IDENTITY_FIELDS = ("gameName", "tagLine", "puuid", "summonerId", "assignedPosition")


def main() -> int:
    import sys

    from draftassist.config import DATA_ROOT

    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_ROOT / "fixtures" / "lcu" / "recorded"
    report = identity_report(folder)
    if not report:
        print(f"No recorded champ selects in {folder}. Play a game with the app open (recording is on by default).")
        return 1
    names = {420: "Solo/Duo", 440: "Flex"}
    for queue, fields in report.items():
        n = fields.pop("sessions")
        print(f"\nQueue {queue} ({names.get(queue, 'other: Ranked 5s?')}): {n} sessions")
        for key in IDENTITY_FIELDS:
            print(f"  {key:<18} {'set in ' + str(fields.get(key, 0)) + ' sessions' if fields.get(key) else 'never set'}")
        others = [k for k in fields if k not in IDENTITY_FIELDS]
        print(f"  other fields set: {', '.join(others) or 'none'}")
    print("\nScouting can be automatic for a queue whose theirTeam has gameName and tagLine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
