"""The Strategy view's builds (docs/strategy.md): data/builds.summarize per (champion, role, opponent), cached for the
app's life (new crawl data shows after a restart or a stats refresh), with the Data Dragon names."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine

from draftassist.engine.strategy import BuildView

log = logging.getLogger(__name__)


class StrategyBuilds:
    def __init__(self, db: Engine, cache_dir: Path, patch: str, brackets: dict[str, str], version: str | None = None,
                 names: dict[int, str] | None = None, opgg=None):
        self.db, self.cache_dir, self.patch, self.brackets, self.version = db, cache_dir, patch, brackets, version
        self.names = names or {}  # champion id -> display name, for OP.GG
        self.opgg = opgg  # data/opgg_builds.OpggBuilds, or None (tests, no network)
        self._static = None
        self._master: object = False  # not looked up yet
        self._cache: dict[tuple, BuildView | None] = {}
        self._thresholds: object = False  # not computed yet

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

    def thresholds(self):
        """What a damage-heavy team is, from the crawl (data/builds.damage_thresholds); None: the engine's defaults."""
        if self._thresholds is False:
            from draftassist.data.builds import damage_thresholds

            try:
                self._thresholds = damage_thresholds(self.db)
            except Exception:
                log.exception("damage thresholds unavailable; the Strategy view uses its defaults")
                self._thresholds = None
        return self._thresholds

    def clear(self) -> None:
        self._cache.clear()
        self._master = False

    def __call__(self, champ: int, role: str, opponent: int | None) -> BuildView | None:
        bracket = self.brackets.get(role, "gold")
        key = (champ, role, opponent, bracket)
        if key not in self._cache:
            from draftassist.data.builds import summarize

            summary = summarize(self.db, self.master(), self.patch, champ, role, bracket, opponent)
            self._cache[key] = BuildView(summary, self.static, self.thresholds()) if summary is not None else None
        return self._cache[key]

    def order_for(self, champ: int, role: str) -> dict | None:
        """Our crawl's item and skill order (data/builds.summarize_order), cached like the builds."""
        key = ("order", champ, role, self.brackets.get(role, "gold"))
        if key not in self._cache:
            from draftassist.data.builds import summarize_order

            try:
                self._cache[key] = summarize_order(self.db, self.master(), self.patch, champ, role, key[3], self.static)
            except Exception:
                log.exception("build order unavailable")
                self._cache[key] = None
        return self._cache[key]

    def opgg_for(self, champ: int, role: str, opponent: int | None) -> dict | None:
        """OP.GG's analysis for the player's tier, and the matchup guide once their opponent is known; None until in
        (fetched in the background, the screen refreshes when they arrive)."""
        order = self.order_for(champ, role) if self.db is not None else None
        if self.opgg is None or champ not in self.names:
            return {"order": order, "loading": False, "analysis": None, "matchup": None} if order else None
        bracket = self.brackets.get(role, "gold")
        analysis = self.opgg.analysis(self.names[champ], role, bracket)
        matchup = self.opgg.matchup(self.names[champ], self.names[opponent], role) if opponent in self.names else None
        if analysis is None and matchup is None:
            return {"loading": True, "tier": bracket, "order": order}
        static = self.static
        for block in (analysis, matchup):
            spells = (block or {}).get("spells")
            if spells and not spells.get("names"):
                spells["names"] = [static.spell_name(i) for i in spells.get("ids") or []]
            runes = (block or {}).get("runes")
            if runes and runes.get("shards") and all(isinstance(x, int) for x in runes["shards"]):
                runes["shards"] = [static.rune_name(x) if x in static.runes else str(x) for x in runes["shards"]]
        return {"tier": bracket if analysis is not None else None, "analysis": analysis, "matchup": matchup,
                "loading": False, "order": order}

    def power_for(self, champ: int, role: str, opponent: int | None) -> dict | None:
        """Lane power by milestone for the champion and, once known, their lane opponent (data/power.py writes it)."""
        if self.db is None:
            return None
        key = ("power", champ, role, opponent)
        if key in self._cache:
            return self._cache[key]
        from sqlalchemy import text

        def curve(c: int) -> list[dict] | None:
            try:
                with self.db.connect() as conn:
                    rows = conn.execute(text(
                        "SELECT idx, label, level, items, theory, measured, n, combined, spike FROM power_curves "
                        "WHERE champ_id = :c AND role = :r ORDER BY idx"), {"c": c, "r": role}).all()
            except Exception:  # a database from before power_curves
                return None
            return [{"label": r[1], "level": r[2], "items": r[3], "theory": r[4], "measured": r[5], "n": r[6],
                     "combined": r[7], "spike": bool(r[8])} for r in rows] or None

        ours = curve(champ)
        theirs = curve(opponent) if opponent else None
        out = None
        if ours:
            from draftassist.engine.power import power_summary

            out = {"ours": ours, "theirs": theirs, **power_summary(ours, theirs)}
        self._cache[key] = out
        return out
