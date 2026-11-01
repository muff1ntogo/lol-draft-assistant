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
