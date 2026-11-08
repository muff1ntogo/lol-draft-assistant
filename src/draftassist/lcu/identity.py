"""Filling in Riot ID tags the champ select session leaves out (docs/scouting-research.md).

Ranked 5s shows the enemy's names, and the client shows their tags only on hover. If the session carries a player's
`gameName` and `puuid` but no `tagLine`, the tag is looked up with a read-only GET on the client's own summoner
endpoints (no Riot API call, nothing written). Each PUUID is asked once per app run; a failed lookup leaves the player
unnamed for scouting (Live then asks for their full name in the Scout opponents box).
"""
from __future__ import annotations

import copy
import logging
from typing import Any

log = logging.getLogger(__name__)

# Tried in order; the first that answers with a tagLine wins. Both are GET-only summoner lookups by the client's PUUID.
SUMMONER_BY_PUUID = ("/lol-summoner/v2/summoners/puuid/{puuid}", "/lol-summoner/v1/summoners/by-puuid-cached/{puuid}")
TEAMS = ("theirTeam", "myTeam")


def needs_tag(member: dict) -> bool:
    return bool(member.get("gameName")) and not member.get("tagLine") and bool(member.get("puuid"))


class TagFiller:
    def __init__(self) -> None:
        self._cache: dict[str, str | None] = {}  # client PUUID -> tag, None when no endpoint knew it

    async def _lookup(self, lcu: Any, puuid: str) -> str | None:
        if puuid in self._cache:
            return self._cache[puuid]
        tag = None
        for template in SUMMONER_BY_PUUID:
            try:
                body = await lcu.get(template.format(puuid=puuid))
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("tagLine"):
                tag = str(body["tagLine"])
                break
        if tag is None:
            log.info("no tag found for a champ select player (client lookups returned nothing)")
        self._cache[puuid] = tag
        return tag

    async def fill(self, lcu: Any, session: dict | None) -> dict | None:
        """The session with any missing tags filled in (a copy when something changed; the original otherwise)."""
        if not isinstance(session, dict) or not any(
                needs_tag(m) for t in TEAMS for m in session.get(t) or [] if isinstance(m, dict)):
            return session
        out = copy.deepcopy(session)
        for team in TEAMS:
            for m in out.get(team) or []:
                if isinstance(m, dict) and needs_tag(m):
                    tag = await self._lookup(lcu, m["puuid"])
                    if tag:
                        m["tagLine"] = tag
        return out
