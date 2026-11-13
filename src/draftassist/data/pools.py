"""Our five players and their champion pools.

Pool import format (spec, Data model): plain text, one block per player, headed by the role;
each line `Champion: tier` with tier main | comfortable | learning.

    TOP
    K'Sante: main
    Rumble: comfortable

Players are keyed by PUUID. Until a Riot ID is resolved (account-v1, or seen in champ select) a
player's PUUID is a placeholder "unresolved:<role>" (":2", ":3"... when taken), so pools can be imported before setup.

Each role has one active player. A player swapped out for a sub goes to the bench with their pool, and comes back
with it (docs/player-roster.md). Everything the engine and Live read is the active players only.

CLI: `uv run python -m draftassist.data.pools import pools.txt`
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, delete, select

from draftassist.db.models import Champion, Player, PlayerGame, PoolEntry
from draftassist.engine.model import ROLES, TIERS, OurTeam, PoolChamp, tier_comfort

log = logging.getLogger(__name__)

ROLE_ALIASES = {
    "top": "top", "jungle": "jungle", "jg": "jungle", "jungler": "jungle", "mid": "mid", "middle": "mid",
    "adc": "adc", "bot": "adc", "bottom": "adc", "carry": "adc", "support": "support", "sup": "support",
    "supp": "support", "utility": "support",
}
DEFAULT_BRACKETS = {"top": "gold", "jungle": "emerald", "mid": "gold", "adc": "gold", "support": "iron"}


def placeholder_puuid(role: str) -> str:
    return f"unresolved:{role}"


def _free_placeholder(s: Session, role: str) -> str:
    """A placeholder key no player has yet: "unresolved:top", else "unresolved:top:2", ..."""
    key, n = placeholder_puuid(role), 1
    while s.get(Player, key) is not None:
        n += 1
        key = f"{placeholder_puuid(role)}:{n}"
    return key


def is_client_puuid(puuid: str) -> bool:
    """The League client's PUUIDs are plain UUIDs (36 chars); the Riot API's are encrypted (78)."""
    return len(puuid) == 36 and puuid.count("-") == 4


def backfill_player_ids(db: Engine) -> None:
    """Players linked before both ids were kept: a client PUUID key is also their client_puuid.
    An API PUUID key is not copied to api_puuid: it may be from an earlier Riot API key (PUUIDs are
    encrypted per app, and sync_api_key clears api_puuid on a key change), so the learning job looks
    api_puuid up from the Riot ID with the current key instead."""
    with Session(db) as s:
        for p in s.exec(select(Player)).all():
            if p.resolved and is_client_puuid(p.puuid) and not p.client_puuid:
                p.client_puuid = p.puuid
                s.add(p)
        s.commit()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ---------- parsing ----------
@dataclass
class ParsedPools:
    entries: dict[str, list[tuple[int, str]]] = field(default_factory=dict)  # role -> [(champ_id, tier)]
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.entries)


def parse_pool_text(text: str, champions: Mapping[int, tuple[str, str]]) -> ParsedPools:
    """`champions`: champ_id -> (Data Dragon key, display name). Collects every error instead of
    stopping at the first, so one paste shows everything to fix."""
    lookup: dict[str, int] = {}
    for cid, (key, name) in champions.items():
        lookup[_norm(name)] = cid
        lookup[_norm(key)] = cid
    display = {_norm(name): name for _, name in champions.values()}
    out = ParsedPools()
    role: str | None = None
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, colon, rest = line.partition(":")
        if not colon or (not rest.strip() and head.strip().lower() in ROLE_ALIASES):  # "TOP" or "Top:"
            r = ROLE_ALIASES.get(head.strip().lower())
            if r is None:
                out.errors.append(f"line {n}: '{line}' is not a role (TOP, JUNGLE, MID, ADC, SUPPORT)")
                role = None
            elif r in out.entries:
                out.errors.append(f"line {n}: {line} appears twice")
                role = None
            else:
                role = r
                out.entries[r] = []
            continue
        if role is None:
            if not out.errors or not out.errors[-1].startswith(f"line {n}"):
                out.errors.append(f"line {n}: '{line}' comes before a role header")
            continue
        name, _, tier = (part.strip() for part in line.partition(":"))
        tier = tier.lower()
        if tier not in TIERS:
            out.errors.append(f"line {n}: tier '{tier}' for {name} must be one of {', '.join(TIERS)}")
            continue
        champ = lookup.get(_norm(name))
        if champ is None:
            close = difflib.get_close_matches(_norm(name), list(display), n=1, cutoff=0.6)
            hint = f" (did you mean {display[close[0]]}?)" if close else ""
            out.errors.append(f"line {n}: unknown champion '{name}'{hint}")
            continue
        if any(c == champ for c, _ in out.entries[role]):
            out.errors.append(f"line {n}: {name} is listed twice for {role}")
            continue
        out.entries[role].append((champ, tier))
    if not out.entries and not out.errors:
        out.errors.append("nothing to import")
    return out


# ---------- players ----------
def player_for_role(s: Session, role: str) -> Player | None:
    """The role's active player."""
    return s.exec(select(Player).where(Player.role == role, Player.active == True)).first()


def active_players(s: Session) -> list[Player]:
    return list(s.exec(select(Player).where(Player.active == True)).all())


def _by_riot_id(s: Session, riot_id: str) -> Player | None:
    if not riot_id:
        return None
    return next((p for p in s.exec(select(Player)).all() if p.riot_id.lower() == riot_id.lower()), None)


# ---------- a player's roles (v1.0.2: one player, several roles, a pool in each) ----------
def roles_of(player: Player, pool_roles: Iterable[str] = ()) -> list[str]:
    """Every role the player plays, in ROLES order: the roles stored for them, the role they play now, and any role
    they have a pool in (a pool in a role means they play it)."""
    have = {r for r in (player.roles or "").split(",") if r} | {player.role} | set(pool_roles)
    return [r for r in ROLES if r in have]


def _pool_roles(s: Session, puuid: str) -> set[str]:
    return {e.role for e in s.exec(select(PoolEntry).where(PoolEntry.puuid == puuid)).all()}


def _roles(s: Session, player: Player) -> list[str]:
    return roles_of(player, _pool_roles(s, player.puuid))


def _play(s: Session, player: Player, role: str) -> None:
    """Make `role` the one they play now, keeping it among their roles."""
    player.roles = ",".join(roles_of(player, [*_pool_roles(s, player.puuid), role]))
    player.role = role


def player_roles(db: Engine) -> dict[str, list[str]]:
    """Every player's roles by internal key."""
    with Session(db) as s:
        pools: dict[str, set[str]] = {}
        for e in s.exec(select(PoolEntry)).all():
            pools.setdefault(e.puuid, set()).add(e.role)
        return {p.puuid: roles_of(p, pools.get(p.puuid, ())) for p in s.exec(select(Player)).all()}


def set_player_roles(db: Engine, riot_id: str, roles: Iterable[str]) -> list[str] | None:
    """Set the roles a player plays. A role taken away takes its pool with it. The role an active player plays now
    can't be taken away (swap them out of it first); a benched player's last role moves to the first one left.
    Returns their roles, or None if there is no such player."""
    chosen = {_role(r) for r in roles}
    if not chosen:
        raise ValueError("a player needs at least one role")
    with Session(db) as s:
        player = _by_riot_id(s, riot_id)
        if player is None:
            return None
        if player.active and player.role not in chosen:
            raise ValueError(f"{player.riot_id} plays {player.role} now: swap them out of it first")
        for role in _pool_roles(s, player.puuid) - chosen:
            s.exec(delete(PoolEntry).where(PoolEntry.puuid == player.puuid, PoolEntry.role == role))
        ordered = [r for r in ROLES if r in chosen]
        if player.role not in chosen:
            player.role = ordered[0]
        player.roles = ",".join(ordered)
        s.add(player)
        s.commit()
        return ordered


def _new_player(s: Session, role: str, riot_id: str = "", bracket: str | None = None) -> Player:
    player = Player(puuid=_free_placeholder(s, role), riot_id=riot_id, role=role, roles=role, rank="",
                    bracket=bracket or DEFAULT_BRACKETS[role])
    s.add(player)
    s.flush()
    return player


def _ensure_player(s: Session, role: str) -> Player:
    return player_for_role(s, role) or _new_player(s, role)


def _bench(s: Session, player: Player) -> None:
    """Off the active roster, pool kept. An empty placeholder (no Riot ID, no pool) is just dropped."""
    has_pool = s.exec(select(PoolEntry).where(PoolEntry.puuid == player.puuid)).first() is not None
    if not player.riot_id and not has_pool:
        s.delete(player)
    else:
        player.active = False
        s.add(player)
    s.flush()


def _resolve(s: Session, player: Player, resolver) -> Player:
    """Link a new player's Riot ID to its PUUID (account-v1) if we can; unresolved otherwise."""
    if not player.riot_id or resolver is None or player.resolved:
        return player
    try:
        puuid = resolver(player.riot_id)
    except Exception as e:  # key expired, network: stay unresolved, champ select can fill it in
        log.warning("could not resolve %s: %s", player.riot_id, e)
        puuid = None
    if puuid and s.get(Player, puuid) is None:
        player = _move_player(s, player, puuid)
        player.api_puuid = puuid
        s.add(player)
    return player


def _swap_in(s: Session, role: str, riot_id: str, resolver=None, bracket: str | None = None) -> Player:
    """Make `riot_id` the role's active player: a benched player comes back with their pool, someone new starts with
    an empty one. The current player goes to the bench. A player already in the lineup moves over from their role:
    the current player takes the role they left if they play it (the two trade), else goes to the bench and the role
    left is empty."""
    current = player_for_role(s, role)
    known = _by_riot_id(s, riot_id)
    left = known.role if known is not None and known.active and known.role != role else None
    if current is not None and (known is None or known.puuid != current.puuid):
        if left is not None and left in _roles(s, current):
            current.role, current.active_since = left, datetime.now(UTC)  # they trade roles
            s.add(current)
            s.flush()
        else:
            _bench(s, current)
    if known is None:
        return _resolve(s, _new_player(s, role, riot_id, bracket), resolver)
    # their pool entries keep their own roles: an old role's pool waits there
    _play(s, known, role)
    known.active, known.active_since = True, datetime.now(UTC)
    s.add(known)
    s.flush()
    return known


def _move_player(s: Session, old: Player, new_puuid: str) -> Player:
    """Re-key a player (e.g. placeholder -> real PUUID), carrying their pool entries, games and profile data along."""
    if old.puuid == new_puuid:
        return old
    new = Player(puuid=new_puuid, riot_id=old.riot_id, role=old.role, roles=old.roles, rank=old.rank, bracket=old.bracket,
                 client_puuid=old.client_puuid, api_puuid=old.api_puuid, active=old.active, active_since=old.active_since,
                 rank_json=old.rank_json, mastery_json=old.mastery_json, profile_fetched_at=old.profile_fetched_at)
    s.add(new)
    s.flush()
    for e in s.exec(select(PoolEntry).where(PoolEntry.puuid == old.puuid)).all():
        s.add(PoolEntry(**{**e.model_dump(), "puuid": new_puuid}))
        s.delete(e)
    s.flush()
    s.delete(old)
    s.flush()
    return new


def _check_riot_id(riot_id: str) -> str:
    riot_id = riot_id.strip()
    if riot_id and "#" not in riot_id:
        raise ValueError(f"Riot ID {riot_id!r} must look like Name#TAG")
    return riot_id


def _role(value) -> str:
    role = ROLE_ALIASES.get(str(value).lower())
    if role is None:
        raise ValueError(f"unknown role {value!r}")
    return role


def set_players(db: Engine, players: list[dict], resolver=None) -> list[dict]:
    """players: [{role, riot_id, rank?, bracket?}]. `resolver(riot_id) -> puuid | None` (account-v1).
    A new Riot ID for a role swaps players when it is someone else: a benched player with that ID comes back with their
    pool, and a linked player (someone real) goes to the bench with theirs. A player never linked is renamed instead,
    pool and all, so fixing a typo doesn't lose the pool. Returns the active players as dicts."""
    with Session(db) as s:
        for p in players:
            role = _role(p["role"])
            riot_id = _check_riot_id(str(p.get("riot_id", "")))
            player = _ensure_player(s, role)
            if riot_id.lower() != player.riot_id.lower():
                known = _by_riot_id(s, riot_id)
                if known is not None and known.puuid != player.puuid:
                    player = _swap_in(s, role, riot_id)  # back from the bench, with their pool
                elif player.resolved and player.riot_id:  # a linked player is someone real: bench them
                    if riot_id:
                        player = _swap_in(s, role, riot_id)
                    else:
                        _bench(s, player)
                        player = _ensure_player(s, role)
                else:
                    player.riot_id = riot_id  # never linked: a rename (a typo fixed keeps the pool)
            player.rank = str(p.get("rank") or player.rank)
            player.bracket = str(p.get("bracket") or player.bracket or DEFAULT_BRACKETS[role]).lower()
            s.add(player)
            s.flush()
            _resolve(s, player, resolver)
        s.commit()
    return list_players(db)


def swap_player(db: Engine, role: str, riot_id: str, resolver=None) -> list[dict]:
    """Put `riot_id` in as the role's player (a sub, or someone back from the bench); the current one is benched with
    their pool. Returns the active players."""
    role = _role(role)
    riot_id = _check_riot_id(riot_id)
    if not riot_id:
        raise ValueError("a Riot ID is needed to swap a player in")
    with Session(db) as s:
        current = player_for_role(s, role)
        bracket = current.bracket if current is not None else None  # a sub plays in the role's bracket until changed
        known = _by_riot_id(s, riot_id)
        if current is None or known is None or known.puuid != current.puuid:
            _swap_in(s, role, riot_id, resolver, bracket)
        s.commit()
    return list_players(db)


def create_player(db: Engine, riot_id: str, role: str, active: bool, pool: list[dict], fetched=None,
                  roles: Iterable[str] = ()) -> list[dict]:
    """Add a player with the pool the user reviewed (docs/roster-profiles.md, Add player), in one transaction.
    `role` is the one they play first; `roles` any others they play (each pool entry names one of them).
    `fetched` is what onboarding read for them, if anything: their API PUUID, rank, mastery and games (rows shaped like
    `player_games`, with their real match ids, so the learning job's later fetch finds them and adds nothing twice).
    Active: the role's current player goes to the bench. Returns the active players."""
    role, riot_id = _role(role), _check_riot_id(riot_id)
    if not riot_id:
        raise ValueError("a Riot ID is needed to add a player")
    plays = {role} | {_role(r) for r in roles}
    if any(e.get("role", role) not in plays or e.get("tier") not in TIERS for e in pool):
        raise ValueError("pool entries must use one of the player's roles and a known tier")
    if len({(e.get("champ_id"), e.get("role", role)) for e in pool}) != len(pool):
        raise ValueError("a champion can appear only once in a pool")
    with Session(db) as s:
        if _by_riot_id(s, riot_id) is not None:
            raise ValueError(f"{riot_id} is already on the roster")
        for item in pool:
            if not s.get(Champion, item.get("champ_id")):
                raise ValueError(f"unknown champion {item.get('champ_id')}")
        current = player_for_role(s, role) if active else None
        if current is not None:
            _bench(s, current)
            bracket = current.bracket
        else:
            bracket = None
        player = _new_player(s, role, riot_id, bracket)
        player.active = active
        player.roles = ",".join(r for r in ROLES if r in plays)
        if fetched is not None and fetched.api_puuid:
            player.api_puuid = fetched.api_puuid
            player.rank_json, player.mastery_json = json.dumps(fetched.ranks), json.dumps(fetched.mastery)
            player.profile_fetched_at = datetime.now(UTC)
            for row in fetched.games:
                s.add(PlayerGame(puuid=player.puuid, source="match-v5", **row))
        s.add(player)
        for item in pool:
            s.add(PoolEntry(puuid=player.puuid, champ_id=item["champ_id"], role=item.get("role", role),
                            manual_tier=item["tier"], comfort=tier_comfort(item["tier"])))
        s.commit()
    return list_players(db)


def bench(db: Engine) -> list[dict]:
    """Benched players by role then name, with their pools per role: [(champ_id, tier)], best tier first."""
    with Session(db) as s:
        out = []
        for p in s.exec(select(Player).where(Player.active == False)).all():
            entries = s.exec(select(PoolEntry).where(PoolEntry.puuid == p.puuid)).all()
            pools: dict[str, list[tuple[int, str]]] = {}
            for e in sorted(entries, key=lambda e: (TIERS.index(e.manual_tier) if e.manual_tier in TIERS else 9,
                                                     e.champ_id)):
                pools.setdefault(e.role, []).append((e.champ_id, e.manual_tier))
            out.append({"riot_id": p.riot_id, "role": p.role, "roles": roles_of(p, pools), "rank": p.rank,
                        "bracket": p.bracket, "resolved": p.resolved, "pools": pools})
    return sorted(out, key=lambda p: (ROLES.index(p["role"]) if p["role"] in ROLES else 9, p["riot_id"].lower()))


def bench_player(db: Engine, riot_id: str) -> bool:
    """Move one active, named player to the bench; they keep their pools and games. False if there is no such player."""
    with Session(db) as s:
        p = _by_riot_id(s, riot_id)
        if p is None or not p.active:
            return False
        _bench(s, p)
        s.commit()
    return True


def delete_player(db: Engine, riot_id: str) -> bool:
    """Remove a player for good: the player, their pools and the games fetched for them. The team's history stays
    (docs/roster-profiles.md, decision 6): drafts, execution rows and the games logged from our own drafts
    (`source == "logged"`), which History and the Team page read. False if there is no such player."""
    with Session(db) as s:
        p = _by_riot_id(s, riot_id)
        if p is None:
            return False
        s.exec(delete(PoolEntry).where(PoolEntry.puuid == p.puuid))
        s.exec(delete(PlayerGame).where(PlayerGame.puuid == p.puuid, PlayerGame.source != "logged"))
        s.delete(p)
        s.commit()
    return True


def lineup(db: Engine) -> dict[str, str]:
    """Role -> the active player's Riot ID ("" when empty): what `set_lineup` puts back."""
    return {p["role"]: p["riot_id"] for p in list_players(db)}


def set_lineup(db: Engine, wanted: Mapping[str, str]) -> None:
    """Put a lineup back exactly (an Undo after a swap or a move): each named player active in their role, everyone
    else who is active now on the bench. Roles left out or "" end up empty."""
    with Session(db) as s:
        chosen: dict[str, Player] = {}
        for role, riot_id in wanted.items():
            role = _role(role)
            if not riot_id:
                continue
            p = _by_riot_id(s, riot_id)
            if p is None:
                raise ValueError(f"no player called {riot_id}")
            chosen[role] = p
        if len({p.puuid for p in chosen.values()}) != len(chosen):
            raise ValueError("one player can play only one role at a time")
        keep = {p.puuid for p in chosen.values()}
        for p in active_players(s):
            if p.puuid not in keep:
                _bench(s, p)
        for role, p in chosen.items():
            if not (p.active and p.role == role):
                _play(s, p, role)
                p.active, p.active_since = True, datetime.now(UTC)
                s.add(p)
        s.commit()


def resolve_from_session(db: Engine, members: list[dict]) -> int:
    """Fill in PUUIDs from champ select teammates (`gameName`, `tagLine`, `puuid`). Returns how many."""
    by_riot_id = {f"{m.get('gameName', '')}#{m.get('tagLine', '')}".lower(): m.get("puuid")
                  for m in members if m.get("puuid") and m.get("gameName")}
    if not by_riot_id:
        return 0
    resolved = 0
    with Session(db) as s:
        for player in s.exec(select(Player)).all():
            puuid = by_riot_id.get(player.riot_id.lower()) if player.riot_id else None
            if not puuid or player.client_puuid == puuid:
                continue
            if not player.resolved and s.get(Player, puuid) is None:
                player = _move_player(s, player, puuid)
            player.client_puuid = puuid
            s.add(player)
            resolved += 1
        s.commit()
    return resolved


def list_players(db: Engine) -> list[dict]:
    with Session(db) as s:
        rows = {p.role: p for p in active_players(s)}
    return [{"role": r, "riot_id": rows[r].riot_id if r in rows else "", "rank": rows[r].rank if r in rows else "",
             "bracket": rows[r].bracket if r in rows else DEFAULT_BRACKETS[r],
             "resolved": rows[r].resolved if r in rows else False} for r in ROLES]


@dataclass(frozen=True)
class Registered:
    """A roster player as the Review pages filter by: their Riot ID, whether they are in the lineup and in which role,
    and their pool per role."""
    riot_id: str
    active: bool
    role: str
    pools: dict[str, frozenset[int]]

    @property
    def name(self) -> str:
        return self.riot_id.split("#")[0]


@dataclass(frozen=True)
class Roster:
    """Who is on the roster (lineup and bench), for keeping the Review pages to them (v1.0.2)."""
    players: tuple[Registered, ...]

    def named(self, name: str | None, role: str) -> Registered | None:
        """The roster player a logged in-game name (or Riot ID) is. A draft that didn't log a name is the role's
        active player's."""
        if not name:
            return next((p for p in self.players if p.active and p.role == role), None)
        key = name.split("#")[0].strip().lower()
        return next((p for p in self.players if p.name.lower() == key), None)

    def in_pool(self, player: Registered, role: str, champ_id: int) -> bool:
        return champ_id in player.pools.get(role, ())


def roster(db: Engine) -> Roster:
    with Session(db) as s:
        pools: dict[tuple[str, str], set[int]] = {}
        for e in s.exec(select(PoolEntry)).all():
            pools.setdefault((e.puuid, e.role), set()).add(e.champ_id)
        players = tuple(Registered(p.riot_id, p.active, p.role,
                                   {r: frozenset(c) for (puuid, r), c in pools.items() if puuid == p.puuid})
                        for p in s.exec(select(Player)).all() if p.riot_id)
    return Roster(players)


def roles_by_puuid(db: Engine) -> dict[str, str]:
    """Champ select PUUID (the client's) -> role, for the active players."""
    with Session(db) as s:
        out = {}
        for p in active_players(s):
            for puuid in (p.client_puuid, p.puuid if p.resolved else None):
                if puuid:
                    out[puuid] = p.role
        return out


class PlayerDirectoryDb:
    """The live service's view of our players, cached until something changes."""

    def __init__(self, db: Engine):
        self.db = db
        self._roles: dict[str, str] | None = None

    def roles_by_puuid(self) -> dict[str, str]:
        if self._roles is None:
            self._roles = roles_by_puuid(self.db)
        return self._roles

    def resolve_from_session(self, members: list[dict]) -> int:
        n = resolve_from_session(self.db, members)
        if n:
            self.invalidate()
        return n

    def invalidate(self) -> None:
        self._roles = None


# ---------- pools ----------
def import_pools(db: Engine, parsed: ParsedPools) -> dict[str, int]:
    """Replace the pool of every role in `parsed`. Keeps scored tier, lock and game history for
    champions that stay in a pool. Returns entries per role."""
    if not parsed.ok:
        raise ValueError("; ".join(parsed.errors))
    counts = {}
    with Session(db) as s:
        for role, entries in parsed.entries.items():
            player = _ensure_player(s, role)
            old = {e.champ_id: e for e in s.exec(select(PoolEntry).where(PoolEntry.puuid == player.puuid,
                                                                         PoolEntry.role == role)).all()}
            s.exec(delete(PoolEntry).where(PoolEntry.puuid == player.puuid, PoolEntry.role == role))
            for champ, tier in entries:
                keep = old.get(champ)
                s.add(PoolEntry(puuid=player.puuid, champ_id=champ, role=role, manual_tier=tier,
                                scored_tier=keep.scored_tier if keep else None,
                                tier_locked=keep.tier_locked if keep else False,
                                comfort=tier_comfort(tier), games=keep.games if keep else 0,
                                wins=keep.wins if keep else 0, last_played=keep.last_played if keep else None))
            counts[role] = len(entries)
        s.commit()
    _recount(db)
    return counts


def _recount(db: Engine) -> None:
    """Games, wins and comfort of every pool entry from the player's stored games, so a champion added
    to a pool shows its record now, not after the next learning run."""
    from draftassist.learning.comfort import rescore

    rescore(db)


def set_pool_entry(db: Engine, role: str, champ_id: int, tier: str) -> None:
    """Add one champion to a player's pool, or change its tier (scored tier, lock and history kept)."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    with Session(db) as s:
        player = _ensure_player(s, role)
        e = s.get(PoolEntry, (player.puuid, champ_id, role))
        if e is None:
            e = PoolEntry(puuid=player.puuid, champ_id=champ_id, role=role, manual_tier=tier,
                          comfort=tier_comfort(tier))
        e.manual_tier = tier
        s.add(e)
        s.commit()
    _recount(db)


def remove_pool_entry(db: Engine, role: str, champ_id: int) -> bool:
    """Take one champion out of a player's pool. False if it wasn't there."""
    with Session(db) as s:
        player = player_for_role(s, role)
        e = s.get(PoolEntry, (player.puuid, champ_id, role)) if player else None
        if e is None:
            return False
        s.delete(e)
        s.commit()
    return True


def set_player_pool_entry(db: Engine, riot_id: str, role: str, champ_id: int, tier: str) -> bool:
    """Add a champion to any player's pool (active or benched, any role), or change its tier. False: no such player."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    with Session(db) as s:
        player = _by_riot_id(s, riot_id)
        if player is None:
            return False
        e = s.get(PoolEntry, (player.puuid, champ_id, role))
        if e is None:
            e = PoolEntry(puuid=player.puuid, champ_id=champ_id, role=role, manual_tier=tier, comfort=tier_comfort(tier))
        e.manual_tier = tier
        s.add(e)
        s.commit()
    return True


def remove_player_pool_entry(db: Engine, riot_id: str, role: str, champ_id: int) -> bool:
    """Take a champion out of any player's pool. False if the player or the entry isn't there."""
    with Session(db) as s:
        player = _by_riot_id(s, riot_id)
        e = s.get(PoolEntry, (player.puuid, champ_id, role)) if player else None
        if e is None:
            return False
        s.delete(e)
        s.commit()
    return True


def load_pools(db: Engine) -> dict[str, list[PoolEntry]]:
    with Session(db) as s:
        players = {p.puuid: p.role for p in active_players(s)}
        out: dict[str, list[PoolEntry]] = {r: [] for r in ROLES}
        for e in s.exec(select(PoolEntry)).all():
            if players.get(e.puuid) == e.role:
                out[e.role].append(e)
    return out


def load_team(db: Engine) -> OurTeam | None:
    """Our team from imported pools (None if nothing imported). Brackets from the players table.
    Comfort comes from the manual tier and the player's recent games with the champion
    (learning/comfort.py); with no games it is the plain tier comfort."""
    from draftassist.learning.comfort import comfort_score, configured_cap

    pools = load_pools(db)
    if not any(pools.values()):
        return None
    brackets = {p["role"]: p["bracket"] for p in list_players(db)}
    cap = configured_cap(db)
    return OurTeam({r: {e.champ_id: PoolChamp(e.manual_tier, comfort_score(e.manual_tier, e.games, e.wins, cap),
                                              e.games, e.wins) for e in entries}
                    for r, entries in pools.items()}, brackets)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from draftassist.config import load_settings
    from draftassist.data.provider import load_catalog
    from draftassist.db.session import init_db

    parser = argparse.ArgumentParser(description="Import champion pools from text")
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("file", type=Path)
    args = parser.parse_args(argv)

    db = init_db(load_settings().db_path)
    parsed = parse_pool_text(args.file.read_text(encoding="utf-8"), load_catalog(db).champs)
    if not parsed.ok:
        print("Not imported:\n  " + "\n  ".join(parsed.errors), file=sys.stderr)
        return 1
    counts = import_pools(db, parsed)
    print("Imported: " + ", ".join(f"{r} {n}" for r, n in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
