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


def init_db(db_path: str | Path | None = None) -> Path:
    """Create the database and apply the schema (idempotent)."""
    if db_path is None:
        db_path = get_settings().db_path
    db_path = Path(db_path)
    conn = connect(db_path)
    try:
        conn.executescript(_read_schema())
        conn.commit()
    finally:
        conn.close()
    log.info("Initialized database at %s", db_path)
    return db_path
