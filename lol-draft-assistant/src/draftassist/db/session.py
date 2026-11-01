"""Engine and schema creation. Schema changes use create_all until a migration tool is needed."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import SQLModel, create_engine

from draftassist.db import models  # noqa: F401  (registers tables on SQLModel.metadata)


def get_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def init_db(db_path: Path) -> Engine:
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    return engine
