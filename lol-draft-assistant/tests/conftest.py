from pathlib import Path

import pytest
from sqlmodel import Session

from draftassist.db.session import init_db

SAMPLE_VERSION = "15.21.1"
SAMPLE_CHAMPIONS = {
    "type": "champion",
    "version": SAMPLE_VERSION,
    "data": {
        "Aatrox": {"id": "Aatrox", "key": "266", "name": "Aatrox", "tags": ["Fighter"]},
        "KSante": {"id": "KSante", "key": "897", "name": "K'Sante", "tags": ["Tank"]},
        "MonkeyKing": {"id": "MonkeyKing", "key": "62", "name": "Wukong", "tags": ["Fighter"]},
    },
}


@pytest.fixture(autouse=True)
def strict_guards(monkeypatch):
    """Suggestion guards raise instead of dropping (engine/guards.py)."""
    monkeypatch.setenv("DRAFTASSIST_STRICT", "1")


@pytest.fixture
def engine(tmp_path: Path):
    return init_db(tmp_path / "test.sqlite")


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


# ---------- live-screen fixtures (real champion ids, synthetic stats) ----------
REAL_NAMES = {
    266: "Aatrox", 103: "Ahri", 84: "Akali", 12: "Alistar", 32: "Amumu", 34: "Anivia", 1: "Annie",
    22: "Ashe", 268: "Azir", 432: "Bard", 53: "Blitzcrank", 63: "Brand", 201: "Braum", 51: "Caitlyn",
    69: "Cassiopeia", 31: "Cho'Gath", 122: "Darius", 119: "Draven", 81: "Ezreal", 9: "Fiddlesticks",
    114: "Fiora", 150: "Gnar", 79: "Gragas", 104: "Graves", 39: "Irelia", 59: "Jarvan IV", 222: "Jinx",
    145: "Kai'Sa", 897: "K'Sante", 64: "Lee Sin", 89: "Leona", 99: "Lux", 111: "Nautilus", 61: "Orianna",
    497: "Rakan", 68: "Rumble", 113: "Sejuani", 163: "Taliyah", 254: "Vi", 110: "Varus",
}


@pytest.fixture
def make_services():
    from draftassist.api.live import LiveHub, LiveService
    from draftassist.api.main import Services
    from draftassist.data.provider import ChampionCatalog
    from draftassist.engine.engine import DraftEngine
    from draftassist.engine.simulate import synthetic_data

    def build(draft_format: str = "auto") -> Services:
        data, team = synthetic_data(names=REAL_NAMES)
        engine = DraftEngine(data, team)
        catalog = ChampionCatalog("16.21.1", {c: (n.replace("'", "").replace(" ", ""), n) for c, n in REAL_NAMES.items()})
        hub = LiveHub()
        live = LiveService(engine, catalog, hub, "fixture", "test data", draft_format)
        return Services(engine, catalog, "fixture", "test data", hub, live, lcu_mode="off")

    return build
