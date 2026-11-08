"""The Strategy view's builds (docs/strategy.md): data/builds.summarize per (champion, role, opponent), cached for the
app's life (new crawl data shows after a restart or a stats refresh), with the Data Dragon names."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine

from draftassist.engine.strategy import BuildView

log = logging.getLogger(__name__)


class StrategyBuilds:
    def __init__(self, db: Engine, cache_dir: Path, patch: str, brackets: dict[str, str], version: str | None = None):
        self.db, self.cache_dir, self.patch, self.brackets, self.version = db, cache_dir, patch, brackets, version
        self._static = None
        self._master: object = False  # not looked up yet
        self._cache: dict[tuple, BuildView | None] = {}

    @property
    def static(self):
        if self._static is None:
            from draftassist.data.builds import load_static

            self._static = load_static(self.cache_dir, self.version)
        return self._static

    def master(self) -> Engine | None:
        if self._master is False:
            from draftassist.data.master import usable_master

            try:
                self._master = usable_master(self.db)
            except Exception:
                log.exception("Master+ database unreadable; builds use the main database alone")
                self._master = None
        return self._master

    def clear(self) -> None:
        self._cache.clear()
        self._master = False

    def __call__(self, champ: int, role: str, opponent: int | None) -> BuildView | None:
        bracket = self.brackets.get(role, "gold")
        key = (champ, role, opponent, bracket)
        if key not in self._cache:
            from draftassist.data.builds import summarize

            summary = summarize(self.db, self.master(), self.patch, champ, role, bracket, opponent)
            self._cache[key] = BuildView(summary, self.static) if summary is not None else None
        return self._cache[key]
