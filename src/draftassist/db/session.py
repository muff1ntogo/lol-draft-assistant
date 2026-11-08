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
# synergy: role-less duos, replaced by synergy_pairs. champion_styles: the comp-style seed, replaced by the tags.
DROPPED_TABLES = ("synergy", "champion_styles")


def drop_obsolete_tables(engine: Engine) -> list[str]:
    insp = inspect(engine)
    dropped = [t for t in DROPPED_TABLES if insp.has_table(t)]
    with engine.begin() as conn:
        for table in dropped:
            conn.execute(text(f"DROP TABLE {table}"))
    return dropped


# ---------- storage layout (docs/shared-data.md, Stage 1; applied by `draftassist db-optimize`, db/optimize.py) ----------
# Tables the models create WITHOUT ROWID (models.WITHOUT_ROWID). A database made before that has them with a rowid and a
# second copy of the key in a sqlite_autoindex; rebuild_table converts one.
WITHOUT_ROWID_TABLES = tuple(t.name for t in SQLModel.metadata.sorted_tables
                             if t.dialect_options["sqlite"].get("with_rowid") is False)
# Indexes older versions created that no query uses (checked with EXPLAIN QUERY PLAN, db/optimize.HOT_QUERIES).
# Since Stage 2 Strategy reads the count tables, so the champion indexes on the per-match build tables went too.
DROPPED_INDEXES = ("ix_match_builds_keystone", "ix_match_builds_champ_id", "ix_match_build_events_champ_id")


def _sqlite_path(engine: Engine) -> str:
    return engine.url.database


def table_sql(engine: Engine, name: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"), {"n": name}).scalar()


def needs_rebuild(engine: Engine) -> list[str]:
    """WITHOUT_ROWID_TABLES that this database still has with a rowid."""
    out = []
    for name in WITHOUT_ROWID_TABLES:
        sql = table_sql(engine, name)
        if sql is not None and "WITHOUT ROWID" not in sql.upper():
            out.append(name)
    return out


def rebuild_table(engine: Engine, name: str) -> None:
    """Recreate a table as its model defines it now (WITHOUT ROWID, the model's indexes), keeping every row: SQLite's
    create-copy-drop-rename procedure, in one transaction with foreign keys off, checked before the commit. Refuses a table
    with columns the model doesn't have, rather than drop them."""
    import re
    import sqlite3

    from sqlalchemy.schema import CreateIndex, CreateTable

    table = SQLModel.metadata.tables[name]
    conn = sqlite3.connect(_sqlite_path(engine), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        existing = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        if not existing:
            raise RuntimeError(f"no table {name}")
        extra = [c for c in existing if c not in table.columns]
        if extra:
            raise RuntimeError(f"{name} has columns the app no longer defines ({', '.join(extra)}): not rebuilt")
        cols = ", ".join(f'"{c}"' for c in existing)
        ddl = str(CreateTable(table).compile(dialect=engine.dialect))
        ddl, n = re.subn(rf'CREATE TABLE "?{re.escape(name)}"?\s*\(', f'CREATE TABLE "_new_{name}" (', ddl, count=1)
        if n != 1:
            raise RuntimeError(f"unexpected DDL for {name}")
        conn.execute("PRAGMA foreign_keys=OFF")  # no effect inside a transaction, so before BEGIN
        conn.execute("BEGIN IMMEDIATE")
        try:
            before = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            conn.execute(ddl)
            conn.execute(f'INSERT INTO "_new_{name}" ({cols}) SELECT {cols} FROM "{name}"')
            conn.execute(f'DROP TABLE "{name}"')
            conn.execute(f'ALTER TABLE "_new_{name}" RENAME TO "{name}"')
            for index in table.indexes:
                conn.execute(str(CreateIndex(index).compile(dialect=engine.dialect)))
            after = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            if after != before:
                raise RuntimeError(f"{name}: {after} rows after the rebuild, {before} before")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError(f"{name}: foreign keys don't check after the rebuild")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.close()


def existing_dropped_indexes(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))}
    return [n for n in DROPPED_INDEXES if n in names]


def drop_indexes(engine: Engine, names) -> None:
    with engine.begin() as conn:
        for n in names:
            conn.execute(text(f'DROP INDEX IF EXISTS "{n}"'))


def init_db(db_path: Path) -> Engine:
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)
    drop_obsolete_tables(engine)
    return engine
