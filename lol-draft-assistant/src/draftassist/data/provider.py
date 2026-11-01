"""Builds the engine's inputs from the database: champion catalog (names, icons), stats, our team.

Until the crawl has stats (Phase 3) and pools are imported (Phase 4), stats and pools are
synthetic, generated over the real champion ids so live drafts show real names and icons.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlmodel import Session, select

from draftassist.data.ddragon import BASE_URL
from draftassist.db.models import Champion
from draftassist.engine.model import EngineData, OurTeam
from draftassist.engine.simulate import synthetic_data

log = logging.getLogger(__name__)

FIXTURE_NOTE = "Synthetic stats and pools (real stats arrive with the Phase 3 crawl, pools with Phase 4)"


@dataclass(frozen=True)
class ChampionCatalog:
    patch: str | None
    champs: Mapping[int, tuple[str, str]]  # champ_id -> (Data Dragon key, display name)

    def card(self, champ_id: int | None) -> dict | None:
        if not champ_id:
            return None
        key, name = self.champs.get(champ_id, (None, f"#{champ_id}"))
        icon = f"{BASE_URL}/cdn/{self.patch}/img/champion/{key}.png" if key and self.patch else None
        return {"id": champ_id, "name": name, "icon": icon}


@dataclass(frozen=True)
class EngineInputs:
    data: EngineData
    team: OurTeam
    catalog: ChampionCatalog
    data_source: str  # "crawl" | "fixture"
    note: str | None


def load_catalog(db: Engine) -> ChampionCatalog:
    with Session(db) as s:
        rows = s.exec(select(Champion)).all()
    patch = max((r.patch for r in rows), default=None, key=lambda v: tuple(int(x) for x in v.split(".")))
    return ChampionCatalog(patch, {r.champ_id: (r.key, r.name) for r in rows})


def load_engine_inputs(db: Engine) -> EngineInputs:
    catalog = load_catalog(db)
    names = {c: name for c, (_, name) in catalog.champs.items()}
    if not names:
        log.warning("champions table is empty; run `uv run python -m draftassist.data.ddragon`")
    data, team = synthetic_data(names=names or None)
    return EngineInputs(data, team, catalog, "fixture", FIXTURE_NOTE)
