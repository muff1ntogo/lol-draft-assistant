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


@pytest.fixture
def engine(tmp_path: Path):
    return init_db(tmp_path / "test.sqlite")


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
