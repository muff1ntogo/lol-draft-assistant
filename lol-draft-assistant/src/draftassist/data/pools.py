"""Our five players and their champion pools.

Pool import format (spec, Data model): plain text, one block per player, headed by the role;
each line `Champion: tier` with tier main | comfortable | learning.

    TOP
    K'Sante: main
    Rumble: comfortable

Players are keyed by PUUID. Until a Riot ID is resolved (account-v1, or seen in champ select) a
player's PUUID is the placeholder "unresolved:<role>", so pools can be imported before setup.

CLI: `uv run python -m draftassist.data.pools import pools.txt`
"""
from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, delete, select

from draftassist.db.models import Champion, Player, PoolEntry
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


def is_client_puuid(puuid: str) -> bool:
    """The League client's PUUIDs are plain UUIDs (36 chars); the Riot API's are encrypted (78)."""
    return len(puuid) == 36 and puuid.count("-") == 4


def backfill_player_ids(db: Engine) -> None:
    """Players linked before both ids were kept: sort the key into client or API PUUID."""
    with Session(db) as s:
        for p in s.exec(select(Player)).all():
            if not p.resolved:
                continue
            if is_client_puuid(p.puuid) and not p.client_puuid:
                p.client_puuid = p.puuid
            elif not is_client_puuid(p.puuid) and not p.api_puuid:
                p.api_puuid = p.puuid
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
    return s.exec(select(Player).where(Player.role == role)).first()


def _ensure_player(s: Session, role: str) -> Player:
    player = player_for_role(s, role)
    if player is None:
        player = Player(puuid=placeholder_puuid(role), riot_id="", role=role, rank="", bracket=DEFAULT_BRACKETS[role])
        s.add(player)
        s.flush()
    return player


def _move_player(s: Session, old: Player, new_puuid: str) -> Player:
    """Re-key a player (e.g. placeholder -> real PUUID), carrying their pool entries along."""
    if old.puuid == new_puuid:
        return old
    new = Player(puuid=new_puuid, riot_id=old.riot_id, role=old.role, rank=old.rank, bracket=old.bracket,
                 client_puuid=old.client_puuid, api_puuid=old.api_puuid)
    s.add(new)
    s.flush()
    for e in s.exec(select(PoolEntry).where(PoolEntry.puuid == old.puuid)).all():
        s.add(PoolEntry(**{**e.model_dump(), "puuid": new_puuid}))
        s.delete(e)
    s.flush()
    s.delete(old)
    s.flush()
    return new


def set_players(db: Engine, players: list[dict], resolver=None) -> list[dict]:
    """players: [{role, riot_id, rank?, bracket?}]. `resolver(riot_id) -> puuid | None` (account-v1).
    Returns the stored players as dicts."""
    with Session(db) as s:
        for p in players:
            role = ROLE_ALIASES.get(str(p["role"]).lower())
            if role is None:
                raise ValueError(f"unknown role {p['role']!r}")
            riot_id = str(p.get("riot_id", "")).strip()
            if riot_id and "#" not in riot_id:
                raise ValueError(f"Riot ID {riot_id!r} must look like Name#TAG")
            player = _ensure_player(s, role)
            if player.riot_id != riot_id and player.resolved:
                player = _move_player(s, player, placeholder_puuid(role))  # different person: unresolve
            player.riot_id = riot_id
            player.rank = str(p.get("rank") or player.rank)
            player.bracket = str(p.get("bracket") or player.bracket or DEFAULT_BRACKETS[role]).lower()
            s.add(player)
            s.flush()
            if riot_id and resolver is not None and not player.resolved:
                try:
                    puuid = resolver(riot_id)
                except Exception as e:  # key expired, network: stay unresolved, champ select can fill it in
                    log.warning("could not resolve %s: %s", riot_id, e)
                    puuid = None
                if puuid:
                    player = _move_player(s, player, puuid)
                    player.api_puuid = puuid
                    s.add(player)
        s.commit()
    return list_players(db)


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
        rows = {p.role: p for p in s.exec(select(Player)).all()}
    return [{"role": r, "riot_id": rows[r].riot_id if r in rows else "", "rank": rows[r].rank if r in rows else "",
             "bracket": rows[r].bracket if r in rows else DEFAULT_BRACKETS[r],
             "resolved": rows[r].resolved if r in rows else False} for r in ROLES]


def roles_by_puuid(db: Engine) -> dict[str, str]:
    """Champ select PUUID (the client's) -> role."""
    with Session(db) as s:
        out = {}
        for p in s.exec(select(Player)).all():
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
    return counts


def load_pools(db: Engine) -> dict[str, list[PoolEntry]]:
    with Session(db) as s:
        players = {p.puuid: p.role for p in s.exec(select(Player)).all()}
        out: dict[str, list[PoolEntry]] = {r: [] for r in ROLES}
        for e in s.exec(select(PoolEntry)).all():
            if players.get(e.puuid) == e.role:
                out[e.role].append(e)
    return out


def load_team(db: Engine) -> OurTeam | None:
    """Our team from imported pools (None if nothing imported). Brackets from the players table.
    Comfort comes from the manual tier and the player's recent games with the champion
    (learning/comfort.py); with no games it is the plain tier comfort."""
    from draftassist.learning.comfort import comfort_score

    pools = load_pools(db)
    if not any(pools.values()):
        return None
    brackets = {p["role"]: p["bracket"] for p in list_players(db)}
    return OurTeam({r: {e.champ_id: PoolChamp(e.manual_tier, comfort_score(e.manual_tier, e.games, e.wins)) for e in entries}
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
