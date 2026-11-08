"""Live progress of a running job for the Crawl page (docs/crawl-page.md): the job writes a small JSON file, the app reads
it. The app sets DRAFTASSIST_PROGRESS to the file's path when it starts a job; run by hand, nothing is written."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ENV = "DRAFTASSIST_PROGRESS"
EVERY_S = 2.0  # at most one write this often, unless forced


class Progress:
    def __init__(self, path: str | Path | None = None, clock=time.time):
        path = path if path is not None else os.environ.get(ENV)
        self.path = Path(path) if path else None
        self._clock = clock
        self._last = 0.0
        self.state: dict = {"started": clock()}

    def update(self, force: bool = False, **fields) -> None:
        self.state.update(fields)
        if self.path is None:
            return
        now = self._clock()
        if not force and now - self._last < EVERY_S:
            return
        self._last = now
        self.state["updated"] = now
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # progress is a nicety: never stop a crawl over it


def read_progress(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------- the crawl makes way for scouting (docs/scouting-research.md) ----------
YIELD_ENV = "DRAFTASSIST_YIELD"
YIELD_FRESH_S = 90.0  # the app refreshes the file on every Live update in a Ranked 5s champ select; older = over
YIELD_MAX_S = 600.0  # never pause longer than this in one go


def request_yield(path: Path, clock=time.time) -> None:
    """The app: a Ranked 5s champ select is scouting, so a running crawl should leave it the key's rate limit."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": clock()}), encoding="utf-8")
    except OSError:
        pass


def yield_requested(path: Path | None, clock=time.time) -> bool:
    if path is None:
        return False
    data = read_progress(path)
    return bool(data) and clock() - float(data.get("at", 0)) < YIELD_FRESH_S


def wait_while_yielding(path: str | Path | None = None, sleep=time.sleep, clock=time.time,
                        on_pause=None) -> float:
    """The crawl: pause while the app asks (at most YIELD_MAX_S). Returns the seconds paused."""
    path = path if path is not None else os.environ.get(YIELD_ENV)
    path = Path(path) if path else None
    start = clock()
    told = False
    while yield_requested(path, clock) and clock() - start < YIELD_MAX_S:
        if not told and on_pause is not None:
            on_pause()
            told = True
        sleep(2.0)
    return clock() - start
