"""SQLite connection factory and schema migration."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from ..config import get_settings
from ..logging import get_logger

log = get_logger(__name__)


def _read_schema() -> str:
    """Load the canonical schema.sql shipped with the package."""
    return resources.files("issuer_data.storage").joinpath("schema.sql").read_text(
        encoding="utf-8"
    )


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sensible PRAGMAs and Row factory."""
    if db_path is None:
        db_path = get_settings().db_path
    db_path = Path(db_path)
    if db_path.parent and str(db_path.parent):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """Add columns the schema gained after a database was first created.

    `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists, so a
    column added to schema.sql never reaches an older database and every write
    naming it fails with "table X has no column named Y". Re-running init-db
    looked like it should fix that and did nothing. ALTER TABLE ADD COLUMN is
    cheap and non-destructive, so bring old databases forward instead.
    """
    added: list[str] = []
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table itself is new; the schema script just created it
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    return added


# (table, column, type) for columns added to schema.sql after initial release.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("filings", "doc_urls", "TEXT"),
)


def init_db(db_path: str | Path | None = None) -> Path:
    """Create the database and apply the schema (idempotent)."""
    if db_path is None:
        db_path = get_settings().db_path
    db_path = Path(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(_read_schema())
        added = _add_missing_columns(conn)
        conn.commit()
    finally:
        conn.close()
    if added:
        log.info("Migrated existing database: added %s", ", ".join(added))
    log.info("Initialized database at %s", db_path)
    return db_path
