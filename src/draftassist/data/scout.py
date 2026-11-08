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
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from itertools import permutations
from pathlib import Path

from sqlalchemy import Engine, func
from sqlmodel import Session, select

from draftassist.data.crawler import POSITIONS
from draftassist.data.riot import RiotAuthError
from draftassist.db.models import ScoutGame, ScoutPlayer
from draftassist.engine.model import ROLES
from draftassist.engine.scouting import (
    SCOUT_WEIGHT,
    STALE_MASTERY_DAYS,
    EnemyScouting,
    ScoutedPlayer,
    recency,
)

log = logging.getLogger(__name__)

WINDOW_DAYS = 365  # how far back "their last MAX_GAMES ranked games" may reach (docs/scouting-research.md)
MAX_GAMES = 30  # per player: their last 30 ranked games (decision 8 Nov 2026). A premade's games overlap, so the team
# usually costs far fewer than 5 x 30 match reads: each match is read once and filed under every scouted player in it
FIRST_ROUND = 5  # games per player read before the rest, so all five have something by the bans
PATCH_DECAY = 0.5  # a game one patch older counts half as much in their pool, two patches a quarter (floor below)
PATCH_FLOOR = 0.125
MASTERY_COUNT = 20
REFRESH_S = 6 * 3600  # a player scouted this recently isn't fetched again
ERROR_RETRY_S = 30  # a player whose fetch failed is asked for once more in the game, this long after (Stage E)
MASTERY_ROLE_EVIDENCE = 3.0  # what their mastery counts as, in games, when guessing their role (Stage E)
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
    """A scouted player: games and wins per role and champion (their last MAX_GAMES ranked games), the same weighted by
    patch, and mastery points."""
    riot_id: str
    status: str
    games: dict[str, dict[int, tuple[int, int]]] = field(default_factory=dict)  # role -> champ -> (games, wins)
    mastery: tuple[tuple[int, int], ...] = ()  # (champ, points), highest first
    games_wanted: int = 0
    mastery_last: dict[int, int] = field(default_factory=dict)  # champ -> when they last played it (epoch ms)
    fetched_at: float = 0.0
    weighted: dict[str, dict[int, float]] = field(default_factory=dict)  # role -> champ -> games x patch weight
    this_patch: dict[str, dict[int, int]] = field(default_factory=dict)  # role -> champ -> games on the current patch
    ranks: tuple[dict, ...] = ()  # league-v4 entries

    @property
    def games_read(self) -> int:
        return sum(g for champs in self.games.values() for g, _ in champs.values())


def _find(s: Session, riot_id: str) -> ScoutPlayer | None:
    return s.exec(select(ScoutPlayer).where(func.lower(ScoutPlayer.riot_id) == riot_id.lower())).first()


def _patch_key(patch: str | None) -> tuple[int, int] | None:
    try:
        major, minor = (int(x) for x in str(patch).split(".")[:2])
        return major, minor
    except (TypeError, ValueError):
        return None


def patch_weight(patch: str | None, current: str | None) -> float:
    """PATCH_DECAY per patch behind the current one (down to PATCH_FLOOR); unknown patches count as two behind."""
    a, b = _patch_key(patch), _patch_key(current)
    if b is None:
        return 1.0
    if a is None:
        behind = 2
    elif a[0] == b[0]:
        behind = max(b[1] - a[1], 0)
    else:
        behind = 3 if a < b else 0
    return max(PATCH_DECAY ** behind, PATCH_FLOOR)


def load_pools(db: Engine, riot_ids: Iterable[str], now: float | None = None,
               current_patch: str | None = None) -> dict[str, PlayerPool]:
    """Riot ID (as asked) -> what's known, for those scouted at all: their last MAX_GAMES ranked games within
    WINDOW_DAYS, counted, and weighted by how far behind `current_patch` each game's patch is."""
    since_ms = int(((now if now is not None else time.time()) - WINDOW_DAYS * 86_400) * 1000)
    out = {}
    with Session(db) as s:
        for rid in riot_ids:
            p = _find(s, rid)
            if p is None:
                continue
            games: dict[str, dict[int, tuple[int, int]]] = {}
            weighted: dict[str, dict[int, float]] = {}
            this_patch: dict[str, dict[int, int]] = {}
            if p.api_puuid:
                rows = s.exec(select(ScoutGame).where(ScoutGame.api_puuid == p.api_puuid,
                                                      ScoutGame.game_start_ms >= since_ms)
                              .order_by(ScoutGame.game_start_ms.desc()).limit(MAX_GAMES)).all()
                for g in rows:
                    if not g.role:
                        continue
                    n, w = games.setdefault(g.role, {}).get(g.champ_id, (0, 0))
                    games[g.role][g.champ_id] = (n + 1, w + int(g.win))
                    wr = weighted.setdefault(g.role, {})
                    wr[g.champ_id] = wr.get(g.champ_id, 0.0) + patch_weight(g.patch, current_patch)
                    if current_patch and g.patch == current_patch:
                        tp = this_patch.setdefault(g.role, {})
                        tp[g.champ_id] = tp.get(g.champ_id, 0) + 1
            rows_m = json.loads(p.mastery_json or "[]")  # [champ, points] or, since Stage E, [champ, points, last ms]
            mastery = tuple((int(r[0]), int(r[1])) for r in rows_m)
            last = {int(r[0]): int(r[2]) for r in rows_m if len(r) > 2 and r[2]}
            ranks = tuple(json.loads(p.rank_json or "[]"))
            out[rid] = PlayerPool(p.riot_id, p.status, games, mastery, p.games_wanted, last, p.fetched_at,
                                  weighted, this_patch, ranks)
    return out


def mastery_age(pool: PlayerPool, now: float | None = None) -> tuple[tuple[int, float], ...]:
    """(champ, days since they last played it) for the mastery champions the API dated."""
    now_ms = (now if now is not None else time.time()) * 1000
    return tuple((c, max(now_ms - ms, 0) / 86_400_000) for c, ms in sorted(pool.mastery_last.items()))


def role_evidence(pool: PlayerPool, role_shares: Mapping[int, Mapping[str, float]] | None = None,
                  now: float | None = None) -> dict[str, float]:
    """How much points to each role: their games there, plus their mastery (sqrt points x recency) spread over the
    roles the meta plays each champion in, worth MASTERY_ROLE_EVIDENCE games. Stage E: a player with no games yet
    still gets a role."""
    out = {r: float(sum(g for g, _ in pool.games.get(r, {}).values())) for r in ROLES}
    if role_shares and pool.mastery:
        ages = dict(mastery_age(pool, now))
        spread = dict.fromkeys(ROLES, 0.0)
        for c, pts in pool.mastery:
            w = math.sqrt(max(pts, 0)) * recency(ages.get(c))
            for r, share in role_shares.get(c, {}).items():
                if r in spread:
                    spread[r] += w * share
        total = sum(spread.values())
        if total > 0:
            for r in ROLES:
                out[r] += MASTERY_ROLE_EVIDENCE * spread[r] / total
    return {r: v for r, v in out.items() if v > 0}


_UNPLACED = math.log(1e-3)  # a player left without a role, when there are more players than roles


def _role_guess(pools: Mapping[str, PlayerPool], given: Mapping[str, str | None],
                role_shares: Mapping[int, Mapping[str, float]] | None,
                now: float | None) -> tuple[dict[str, str], dict[str, float]]:
    """(Riot ID -> role, Riot ID -> how sure). The client's positions are certain. The rest: the most likely joint
    assignment of the free roles, scored by sum of log(their share of evidence in the role), so two players who both
    play mostly top don't both claim it; how sure is the softmax marginal over every assignment."""
    roles = {rid: r for rid, r in given.items() if r in ROLES}
    sure = dict.fromkeys(roles, 1.0)
    free = [r for r in ROLES if r not in roles.values()]
    probs = {}
    for rid, pool in pools.items():
        if rid in roles:
            continue
        ev = role_evidence(pool, role_shares, now)
        total = sum(ev.values())
        if total > 0:
            probs[rid] = {r: v / total for r, v in ev.items()}
    players = sorted(probs, key=lambda rid: (-pools[rid].games_read, rid.lower()))
    if not players or not free:
        return roles, sure
    slots = free + [None] * len(players)  # anyone may stay unplaced, at a cost
    scored: dict[tuple, float] = {}
    for perm in permutations(slots, len(players)):
        if perm in scored:
            continue
        total = 0.0
        for rid, r in zip(players, perm):
            if r is None:
                total += _UNPLACED
            elif probs[rid].get(r, 0.0) > 0:
                total += math.log(probs[rid][r])
            else:
                break  # no evidence at all for this role: never put them there
        else:
            scored[perm] = total
    if not scored:
        return roles, sure
    best = max(scored, key=scored.get)  # ties: the first in permutation order, so it's deterministic
    top = scored[best]
    z = sum(math.exp(v - top) for v in scored.values())
    for i, rid in enumerate(players):
        if best[i] is None:
            continue
        roles[rid] = best[i]
        sure[rid] = sum(math.exp(v - top) for perm, v in scored.items() if perm[i] == best[i]) / z
    return roles, sure


def guess_roles(pools: Mapping[str, PlayerPool], given: Mapping[str, str | None],
                role_shares: Mapping[int, Mapping[str, float]] | None = None,
                now: float | None = None) -> tuple[dict[str, str], dict[str, float]]:
    """(Riot ID -> role, Riot ID -> how sure we are of it): `assign_roles` with its certainty."""
    return _role_guess(pools, given, role_shares, now)


def assign_roles(pools: Mapping[str, PlayerPool], given: Mapping[str, str | None],
                 role_shares: Mapping[int, Mapping[str, float]] | None = None,
                 now: float | None = None) -> dict[str, str]:
    """Riot ID -> the role we take them to play: the client's position when it gives one, else the most likely joint
    assignment of the free roles from their games and (with `role_shares`) their mastery. A player with nothing to go
    on gets none."""
    return _role_guess(pools, given, role_shares, now)[0]


def build_scouting(pools: Mapping[str, PlayerPool], given_roles: Mapping[str, str | None],
                   weight: float = SCOUT_WEIGHT, role_shares: Mapping[int, Mapping[str, float]] | None = None,
                   now: float | None = None) -> EnemyScouting | None:
    """What the engine reads (engine/scouting.py), or None if nobody has anything to go on yet."""
    roles, sure = _role_guess(pools, given_roles, role_shares, now)
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
        weighted = tuple(sorted(pool.weighted.get(role, {}).items()))
        players.append(ScoutedPlayer(rid.split("#")[0], role, games, tuple(sorted(any_role.items())), pool.mastery,
                                     mastery_age(pool, now), sure.get(rid, 1.0), weighted))
    return EnemyScouting(tuple(players), weight) if players else None


TOP_SHOWN = 3
ONE_TRICK_SHARE = 0.6  # one champion is this share of their games in the role...
ONE_TRICK_GAMES = 8  # ...over at least this many games


def scout_summary(riot_ids: Iterable[str], pools: Mapping[str, PlayerPool], roles: Mapping[str, str],
                  card: Callable[[int], dict | None], now: float | None = None,
                  sure: Mapping[str, float] | None = None) -> list[dict]:
    """Per player, what Live shows: their name, the role we take them to play (and how sure, from `sure`), how far
    the fetch is, and their top champions in that role (games and win rate), or by mastery until games are in (those
    played lately first; ones untouched for months only if there's nothing else). Labels: `one_trick` (one champion
    is ONE_TRICK_SHARE of ONE_TRICK_GAMES+ games in the role) and `mastery_only` (fetched, but no recent games)."""
    out = []
    for rid in riot_ids:
        pool, role = pools.get(rid), roles.get(rid)
        top: list[dict] = []
        games_in_role, one_trick = 0, None
        if pool is not None:
            played = pool.games.get(role, {}) if role else {}
            games_in_role = sum(g for g, _ in played.values())
            if played and games_in_role >= ONE_TRICK_GAMES:
                c, (g, _) = max(played.items(), key=lambda kv: (kv[1][0], -kv[0]))
                if g / games_in_role >= ONE_TRICK_SHARE:
                    one_trick = card(c)
            if played:
                weighted = pool.weighted.get(role, {})
                this = pool.this_patch.get(role, {})
                # ordered by this patch's weight first, so a champion they moved to lately leads
                for c, (g, w) in sorted(played.items(), key=lambda kv: (-weighted.get(kv[0], kv[1][0]), -kv[1][0],
                                                                        kv[0]))[:TOP_SHOWN]:
                    top.append({"champ": card(c), "games": g, "wins": w, "this_patch": this.get(c, 0)})
            else:
                ages = dict(mastery_age(pool, now))
                ranked = sorted(pool.mastery, key=lambda cp: -math.sqrt(max(cp[1], 0)) * recency(ages.get(cp[0])))
                fresh = [cp for cp in ranked if ages.get(cp[0], 0.0) <= STALE_MASTERY_DAYS] or ranked
                top = [{"champ": card(c), "points": p} for c, p in fresh[:TOP_SHOWN]]
        out.append({"riot_id": rid, "name": rid.split("#")[0], "role": role, "status": pool.status if pool else "new",
                    "games_read": pool.games_read if pool else 0, "games_wanted": pool.games_wanted if pool else 0,
                    "top": top, "games_in_role": games_in_role, "one_trick": one_trick,
                    "mastery_only": bool(pool is not None and pool.status == "done" and not pool.games),
                    "role_sure": None if role is None or sure is None else round(sure.get(rid, 1.0), 3),
                    "rank": rank_label(pool.ranks) if pool else None,
                    "games_this_patch": sum(sum(c.values()) for c in pool.this_patch.values()) if pool else 0})
    return out


RANK_QUEUES = ("RANKED_FLEX_SR", "RANKED_SOLO_5x5")  # shown in this order: Ranked 5s plays out of Flex's ladder or its own


def rank_label(entries: Iterable[dict]) -> str | None:
    """"Gold II" from league-v4 entries: Flex first, else Solo/Duo, else whatever queue there is."""
    entries = list(entries)
    if not entries:
        return None
    order = {q: i for i, q in enumerate(RANK_QUEUES)}
    e = min(entries, key=lambda e: order.get(e.get("queueType"), 9))
    tier = str(e.get("tier", "")).title()
    division = "" if tier in ("Master", "Grandmaster", "Challenger") else f" {e.get('rank', '')}"
    return f"{tier}{division}".strip() or None


# ---------- fetching ----------
def _save(db: Engine, riot_id: str, **values) -> None:
    with Session(db) as s:
        p = _find(s, riot_id) or ScoutPlayer(riot_id=riot_id)
        for k, v in values.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()


def _call(fn, *args):
    """A Riot call for scouting: RiotClient already retries network errors, 5xx and 429s; this adds one more try after a
    short pause for anything else transient, so one hiccup doesn't cost a player's pool."""
    try:
        return fn(*args)
    except RiotAuthError:
        raise
    except Exception:
        time.sleep(1.0)
        return fn(*args)


def scout(db: Engine, riot, riot_ids: Iterable[str], now: float | None = None, force: bool = False,
          progress: Callable[[], None] | None = None) -> dict[str, str]:
    """Fetch the players' mastery (every champion) and rank, then their last MAX_GAMES ranked games. Returns each Riot
    ID's final status. A player scouted within REFRESH_S is skipped unless forced. A bad key stops everything
    (RiotAuthError); any other trouble with one player marks that player "error" and goes on.

    Matches are read once for the whole team: a premade's games overlap, so each match read is filed under every
    scouted player in it, and reading goes round by round (each player's next unread game), FIRST_ROUND rounds first
    so all five have games by the bans. `progress` is called once mastery is in and after each round."""
    tell = progress or (lambda: None)
    now = now if now is not None else time.time()
    since = int(now - WINDOW_DAYS * 86_400)
    ids = list(dict.fromkeys(riot_ids))
    statuses: dict[str, str] = {}
    queue: dict[str, list[str]] = {}  # riot id -> its match ids still to read, newest first
    puuids: dict[str, str] = {}  # riot id -> API PUUID

    for rid in ids:  # 1. who they are, what they've mastered, their rank: two or three quick requests each
        with Session(db) as s:
            known = _find(s, rid)
            fresh = known is not None and known.status in ("done", "missing") and now - known.fetched_at < REFRESH_S
        if fresh and not force:
            statuses[rid] = known.status
            continue
        try:
            puuid = _call(riot.account_puuid, rid)
            if not puuid:
                _save(db, rid, api_puuid=None, status="missing", fetched_at=now)
                statuses[rid] = "missing"
                continue
            fetch_all = getattr(riot, "mastery_all", None)
            rows = _call(fetch_all, puuid) if fetch_all else _call(riot.mastery_top, puuid, MASTERY_COUNT)
            rows = sorted(rows or [], key=lambda m: -m.get("championPoints", 0))
            mastery = [[m["championId"], m["championPoints"], m.get("lastPlayTime") or 0] for m in rows]
            ranks = None
            if (fetch_ranks := getattr(riot, "ranks", None)) is not None:
                try:
                    ranks = [{"queueType": e.get("queueType"), "tier": e.get("tier"), "rank": e.get("rank"),
                              "lp": e.get("leaguePoints"), "wins": e.get("wins"), "losses": e.get("losses")}
                             for e in fetch_ranks(puuid) or []]
                except RiotAuthError:
                    raise
                except Exception:
                    log.warning("rank for %s unavailable", rid)
            _save(db, rid, api_puuid=puuid, mastery_json=json.dumps(mastery), status="mastery", fetched_at=now,
                  **({"rank_json": json.dumps(ranks)} if ranks is not None else {}))
            statuses[rid], puuids[rid] = "mastery", puuid
        except RiotAuthError:
            raise
        except Exception:
            log.exception("scouting %s failed", rid)
            _save(db, rid, status="error", fetched_at=now)
            statuses[rid] = "error"

    tell()
    for rid in list(puuids):  # 2. which games: their last MAX_GAMES ranked games
        try:
            found = list(_call(riot.match_ids, puuids[rid], None, since, MAX_GAMES) or [])[:MAX_GAMES]
            _save(db, rid, status="games", games_wanted=len(found))
            queue[rid] = found
            statuses[rid] = "games"
        except RiotAuthError:
            raise
        except Exception:
            log.exception("scouting %s's match ids failed", rid)
            _save(db, rid, status="error")
            statuses[rid] = "error"
            puuids.pop(rid)

    scouted = set(puuids.values())
    with Session(db) as s:  # already stored for someone in this team: nothing to read
        have = {(g.api_puuid, g.match_id) for g in s.exec(select(ScoutGame).where(
            ScoutGame.api_puuid.in_(scouted))).all()} if scouted else set()
    read: set[str] = set()
    for i in range(MAX_GAMES):  # 3. round by round, each match once for the whole team
        changed = False
        for rid, found in queue.items():
            if i >= len(found) or statuses[rid] == "error":
                continue
            match_id = found[i]
            if match_id in read or (puuids[rid], match_id) in have:
                continue
            read.add(match_id)
            try:
                changed = bool(_read_match(db, riot, scouted, match_id)) or changed
            except RiotAuthError:
                raise
            except Exception:
                log.exception("scouting %s's match %s failed", rid, match_id)
        if changed and (i + 1 >= FIRST_ROUND or i + 1 == min(FIRST_ROUND, max((len(f) for f in queue.values()), default=0))):
            tell()
    for rid in queue:
        if statuses[rid] == "games":
            _save(db, rid, status="done", fetched_at=now)
            statuses[rid] = "done"
    tell()
    return statuses


def _read_match(db: Engine, riot, puuids: set[str] | str, match_id: str) -> set[str]:
    """Store one match for every scouted player in it; the PUUIDs it was stored for (empty: nothing new)."""
    puuids = {puuids} if isinstance(puuids, str) else set(puuids)
    match = _call(riot.match, match_id)
    if not match:
        return set()
    info = match["info"]
    if info.get("gameDuration", 0) < MIN_DURATION_S:
        return set()
    from draftassist.data.crawler import patch_of

    try:
        patch = patch_of(info.get("gameVersion", ""))
    except Exception:
        patch = None
    stored = set()
    with Session(db) as s:
        for me in info["participants"]:
            puuid = me.get("puuid")
            if puuid not in puuids or s.get(ScoutGame, (puuid, match_id)) is not None:
                continue
            s.add(ScoutGame(api_puuid=puuid, match_id=match_id, champ_id=me["championId"],
                            role=POSITIONS.get(me.get("teamPosition") or "")
                            or POSITIONS.get(me.get("individualPosition") or ""),
                            win=bool(me["win"]), queue_id=info.get("queueId"),
                            game_start_ms=info.get("gameStartTimestamp", 0), patch=patch))
            stored.add(puuid)
        s.commit()
    return stored


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
