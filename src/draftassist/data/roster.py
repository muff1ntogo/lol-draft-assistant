"""Our players and their pools, mirrored to a local file (docs/player-roster.md).

The database is the working copy; `data/roster.json` in the data folder is a plain copy of every player (active and
benched) and their pools, written after each change. Updates never touch the data folder, and if the database ever
starts without our players (a new install without `import-data`, a reset database) the roster comes back from the file.

No PUUIDs in the file: players link again from their Riot IDs (account-v1, or the next champ select). Real Riot IDs, so
it stays local like the database (.gitignore).
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.pools import DEFAULT_BRACKETS, _free_placeholder, roles_of
from draftassist.db.models import Player, PoolEntry
from draftassist.engine.model import ROLES, TIERS, tier_comfort

log = logging.getLogger(__name__)

VERSION = 1


def roster_file(data_root: Path | None = None) -> Path:
    if data_root is None:
        from draftassist.config import DATA_ROOT

        data_root = DATA_ROOT
    return Path(data_root) / "data" / "roster.json"


def snapshot(db: Engine, name: Callable[[int], str] | None = None) -> dict:
    """Every player and their pool, as the file holds them."""
    with Session(db) as s:
        players = []
        for p in sorted(s.exec(select(Player)).all(), key=lambda p: (not p.active, p.role, p.riot_id.lower())):
            entries = sorted(s.exec(select(PoolEntry).where(PoolEntry.puuid == p.puuid)).all(),
                             key=lambda e: (e.role, e.champ_id))
            pool = [{"role": e.role, "champ_id": e.champ_id, "tier": e.manual_tier, "locked": e.tier_locked,
                     **({"champion": name(e.champ_id)} if name else {})} for e in entries]
            if not p.riot_id and not pool:
                continue
            players.append({"riot_id": p.riot_id, "role": p.role, "roles": roles_of(p, (e["role"] for e in pool)),
                            "active": p.active, "rank": p.rank, "bracket": p.bracket, "pool": pool})
    return {"version": VERSION, "saved_at": int(time.time()), "players": players}


def save(db: Engine, path: Path, name: Callable[[int], str] | None = None) -> bool:
    """Write the roster file (atomically; the previous one kept as .bak). False if there's nothing worth saving, so an
    empty database never overwrites a good file."""
    data = snapshot(db, name)
    if not data["players"]:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=1, ensure_ascii=False)
    if path.is_file():
        if _players(path) == data["players"]:
            return True
        os.replace(path, path.with_suffix(".json.bak"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _players(path: Path) -> list[dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    players = data.get("players") if isinstance(data, dict) else None
    return players if isinstance(players, list) else None


def restore(db: Engine, path: Path, known_champs: set[int] | None = None) -> int:
    """Bring the roster back from the file when the database has none (no Riot IDs and no pools). Returns how many
    players were restored; 0 if the database already has a roster or the file is missing or unreadable (then the .bak).
    """
    with Session(db) as s:
        if any(p.riot_id for p in s.exec(select(Player)).all()) or s.exec(select(PoolEntry)).first() is not None:
            return 0
    players = _players(path)
    if players is None:
        players = _players(path.with_suffix(".json.bak"))
    if not players:
        return 0
    restored = 0
    with Session(db) as s:
        for old in s.exec(select(Player)).all():  # empty placeholders only (checked above)
            s.delete(old)
        s.flush()
        active_roles: set[str] = set()
        for row in players:
            role = row.get("role")
            if role not in ROLES or not isinstance(row.get("pool", []), list):
                continue
            active = bool(row.get("active", True)) and role not in active_roles
            if active:
                active_roles.add(role)
            roles = [r for r in ROLES if r == role or r in (row.get("roles") or [])]  # files before v1.0.2 have none
            p = Player(puuid=_free_placeholder(s, role), riot_id=str(row.get("riot_id") or ""), role=role,
                       roles=",".join(roles),
                       rank=str(row.get("rank") or ""), bracket=str(row.get("bracket") or DEFAULT_BRACKETS[role]),
                       active=active)
            s.add(p)
            s.flush()
            seen = set()
            for e in row.get("pool", []):
                champ, r, tier = e.get("champ_id"), e.get("role"), e.get("tier")
                if not isinstance(champ, int) or r not in ROLES or tier not in TIERS or (champ, r) in seen:
                    continue
                if known_champs and champ not in known_champs:
                    continue
                seen.add((champ, r))
                s.add(PoolEntry(puuid=p.puuid, champ_id=champ, role=r, manual_tier=tier,
                                tier_locked=bool(e.get("locked")), comfort=tier_comfort(tier)))
            restored += 1
        s.commit()
    log.info("restored %d players and their pools from %s", restored, path)
    return restored
