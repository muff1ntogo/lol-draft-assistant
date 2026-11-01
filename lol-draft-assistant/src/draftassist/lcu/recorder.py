"""Dumps every champ select event to fixtures/lcu/recorded/<start time>/NNNN-<kind>.json.

The spec's day-one task: the first Ranked 5s games show what `theirTeam` and the scouting tooltip
expose, and become replay fixtures for the parser. Recordings contain player names and PUUIDs, so
the folder is git-ignored; copy a session into fixtures/lcu/ to keep it as a committed fixture.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


class SessionRecorder:
    def __init__(self, root: Path):
        self.root = root
        self._dir: Path | None = None
        self._n = 0

    def record(self, kind: str, data: object) -> None:
        try:
            if self._dir is None:
                self._dir = self.root / datetime.now().strftime("%Y%m%d-%H%M%S")
                self._dir.mkdir(parents=True, exist_ok=True)
                self._n = 0
            self._n += 1
            path = self._dir / f"{self._n:04d}-{kind}.json"
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.exception("could not record %s event", kind)

    def end(self) -> None:
        """Champ select ended: the next event starts a new recording folder."""
        self._dir = None


IDENTITY_KEYS = ("gameName", "tagLine", "puuid", "summonerId", "obfuscatedPuuid", "obfuscatedSummonerId",
                 "internalName", "summonerName", "displayName", "playerAlias", "nameVisibilityType")


def sanitize_session(session: dict) -> dict:
    """A champ select session with every player identity replaced by a placeholder per cell, chat
    credentials removed and session/game ids blanked, so a recording can be committed as a fixture."""
    out = {k: v for k, v in session.items() if k != "chatDetails"}
    # The session id doubles as the chat room id, and a game id leads to every player via match-v5.
    if "id" in out:
        out["id"] = "00000000-0000-0000-0000-000000000000"
    if out.get("gameId"):
        out["gameId"] = 1
    for team in ("myTeam", "theirTeam"):
        members = []
        for m in session.get(team, []):
            cell = m.get("cellId", 0)
            clean = {k: v for k, v in m.items() if k not in IDENTITY_KEYS}
            if m.get("puuid"):
                clean["puuid"] = f"puuid-{cell}"
            if m.get("gameName"):
                clean["gameName"], clean["tagLine"] = f"Player{cell}", "EUW"
            members.append(clean)
        out[team] = members
    return out


def identity_values(session: dict) -> set[str]:
    """Every identifying string in a session (to check a sanitized copy leaks none of them)."""
    values = {str(m[k]) for team in ("myTeam", "theirTeam") for m in session.get(team, [])
              for k in IDENTITY_KEYS if m.get(k) not in (None, "", 0) and len(str(m[k])) > 3}
    values |= {v for v in (session.get("chatDetails") or {}).values() if isinstance(v, str) and len(v) > 3}
    values |= {str(session[k]) for k in ("id", "gameId") if session.get(k) and len(str(session[k])) > 3}
    return values
