"""Engine and schema creation. New tables come from create_all; new columns on existing tables
from add_missing_columns (additive only: columns must be nullable or have a default). Derived tables
a newer version no longer uses are dropped (DROPPED_TABLES)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, event, inspect, text
from sqlmodel import SQLModel, create_engine

from draftassist.db import models  # noqa: F401  (registers tables on SQLModel.metadata)


def get_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _configure(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        # WAL: the app keeps reading while the crawler writes and aggregates.
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA busy_timeout=30000")

    return engine


def add_missing_columns(engine: Engine) -> list[str]:
    """ALTER TABLE ... ADD COLUMN for model columns an older database doesn't have yet."""
    added = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                clause = f" DEFAULT {int(default) if isinstance(default, bool) else repr(default)}" if default is not None else ""
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}{clause}'))
                added.append(f"{table.name}.{col.name}")
    return added


# Tables a newer version no longer has. Derived data only (rebuilt by aggregation), so dropping is safe.
DROPPED_TABLES = ("synergy",)  # role-less duos, replaced by synergy_pairs (docs/synergy-visualizer.md)


def drop_obsolete_tables(engine: Engine) -> list[str]:
    insp = inspect(engine)
    dropped = [t for t in DROPPED_TABLES if insp.has_table(t)]
    with engine.begin() as conn:
        for table in dropped:
            conn.execute(text(f"DROP TABLE {table}"))
    return dropped


def init_db(db_path: Path) -> Engine:
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)
    drop_obsolete_tables(engine)
    return engine
