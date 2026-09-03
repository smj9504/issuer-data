"""SQLite connection factory and schema migration."""

from __future__ import annotations

import re
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


def _apply_missing_tables(conn: sqlite3.Connection) -> list[str]:
    """Create tables/views the schema gained since this database was made.

    A *new table* is unlike a new column: `CREATE TABLE IF NOT EXISTS` would
    create it happily, so all that is missing is somebody re-running the schema.
    But nothing prompts that, and the symptom is a bare
    `sqlite3.OperationalError: no such table: scan_progress` thrown partway
    through a collection — which names the table but not the remedy.

    Applying the schema script on connect is cheap (every statement is
    IF NOT EXISTS, and the views are CREATE-after-DROP) and removes a whole class
    of "works on a fresh clone, crashes on mine" reports.
    """
    before = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    conn.executescript(_read_schema())
    after = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    return sorted(after - before)


def connect(db_path: str | Path | None = None, *, ensure_schema: bool = True
            ) -> sqlite3.Connection:
    """Open a SQLite connection with sensible PRAGMAs and Row factory.

    `ensure_schema` brings a database created by an older revision up to date
    (missing tables/views created, missing columns added) so a stale local
    database cannot fail mid-collection on a table it has never heard of. Pass
    False to open a connection without touching the file's structure.
    """
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
    if ensure_schema:
        try:
            created = _apply_missing_tables(conn)
            added = _add_missing_columns(conn)
            rekeyed = _rebuild_changed_keys(conn)
            conn.commit()
            if created or added or rekeyed:
                log.info("Schema brought up to date: %s",
                         ", ".join(created + added + rekeyed))
        except sqlite3.Error as exc:
            # Never make opening a database fatal over a migration; the caller
            # may only be reading, and init-db remains the explicit repair.
            log.warning("Could not auto-apply schema (%s); run init-db", exc)
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


# Tables whose PRIMARY KEY changed after release, with the column whose absence
# from the old key silently dropped rows.
_REKEYED: tuple[tuple[str, str], ...] = (
    ("kr_stake_changes", "stock_kind"),
)


def _rebuild_changed_keys(conn: sqlite3.Connection) -> list[str]:
    """Rebuild tables whose PRIMARY KEY widened since they were created.

    SQLite cannot alter a primary key, and CREATE TABLE IF NOT EXISTS will not
    touch an existing table, so a key fix reaches old databases only by rebuild.
    Leaving it undone is not cosmetic: the narrower key made two genuinely
    different 변동 lines collide, so one was silently overwritten and its
    quantity lost.

    The rebuild carries existing rows over, but it cannot recover what the old key
    already overwrote: that data never reached the table. Those receipts also will
    not be re-fetched on the next run, because collection skips 접수번호 it has
    already stored. Recovering them needs a re-run with --restart over the affected
    period, which the warning below says out loud rather than leaving the table
    quietly short.
    """
    rebuilt: list[str] = []
    for table, key_col in _REKEYED:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or not row[0]:
            continue
        ddl = row[0]
        pk = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", ddl, re.DOTALL)
        if not pk or key_col in pk.group(1):
            continue  # already on the current key
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        collist = ", ".join(cols)
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}__old")
        conn.executescript(_read_schema())
        # DISTINCT because the old key let near-duplicates through; the new key
        # would otherwise reject the copy outright.
        conn.execute(
            f"INSERT OR IGNORE INTO {table} ({collist}) SELECT DISTINCT {collist} "
            f"FROM {table}__old"
        )
        conn.execute(f"DROP TABLE {table}__old")
        rebuilt.append(f"{table} (re-keyed on {key_col})")
        log.warning(
            "%s was rebuilt on a wider primary key. Rows overwritten under the old "
            "key are NOT recoverable from the database; re-run collection for the "
            "affected period to restore them.", table
        )
    return rebuilt
