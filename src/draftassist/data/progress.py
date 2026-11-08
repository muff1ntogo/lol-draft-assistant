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
