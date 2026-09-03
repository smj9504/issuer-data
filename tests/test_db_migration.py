"""init-db must bring an older database forward, not just create a new one.

`CREATE TABLE IF NOT EXISTS` skips a table that already exists, so a column added
to schema.sql later never reaches a database created before it. That surfaced as
"table filings has no column named doc_urls" on every filings write, with
re-running init-db appearing to do nothing.
"""

import sqlite3

from issuer_data.storage.db import _ADDED_COLUMNS, init_db


def _legacy_filings_db(path):
    """A filings table as it was before doc_urls was added."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE companies (company_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE filings (
            company_id  INTEGER NOT NULL,
            filing_id   TEXT NOT NULL,
            filed_date  TEXT,
            filing_type TEXT,
            title       TEXT,
            url         TEXT,
            source      TEXT NOT NULL,
            PRIMARY KEY (company_id, filing_id, source)
        );
        INSERT INTO companies VALUES (1, 'Existing Co');
        INSERT INTO filings VALUES (1, 'acc-1', '2024-01-01', '8-K', 't', 'u', 'edgar');
        """
    )
    conn.commit()
    conn.close()


def test_init_db_adds_column_missing_from_an_old_database(tmp_path):
    db = tmp_path / "legacy.sqlite"
    _legacy_filings_db(db)

    init_db(db)

    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filings)")}
    assert "doc_urls" in cols


def test_migration_preserves_existing_rows(tmp_path):
    db = tmp_path / "legacy.sqlite"
    _legacy_filings_db(db)

    init_db(db)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT filing_id, filing_type FROM filings").fetchall()
    assert rows == [("acc-1", "8-K")]


def test_writes_naming_the_new_column_succeed_after_migration(tmp_path):
    """The actual failure: an INSERT naming doc_urls used to raise OperationalError."""
    db = tmp_path / "legacy.sqlite"
    _legacy_filings_db(db)

    init_db(db)

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO filings(company_id, filing_id, filed_date, filing_type, "
        "title, url, doc_urls, source) VALUES (?,?,?,?,?,?,?,?)",
        (1, "acc-2", "2024-02-02", "F-1", "t", "u", '["http://x"]', "edgar"),
    )
    conn.commit()
    got = conn.execute(
        "SELECT doc_urls FROM filings WHERE filing_id='acc-2'"
    ).fetchone()[0]
    assert got == '["http://x"]'


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy.sqlite"
    _legacy_filings_db(db)

    init_db(db)
    init_db(db)  # must not raise "duplicate column name"

    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(filings)")]
    assert cols.count("doc_urls") == 1


def test_declared_migrations_match_the_shipped_schema(tmp_path):
    """Every column in _ADDED_COLUMNS must actually exist in schema.sql.

    A stale entry here would silently ALTER a fresh database into a shape the
    schema never declared.
    """
    db = tmp_path / "fresh.sqlite"
    init_db(db)

    conn = sqlite3.connect(db)
    for table, column, _decl in _ADDED_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert column in cols, f"{table}.{column} is not in schema.sql"


def test_connect_creates_tables_added_after_the_database_was_made(tmp_path):
    """A new *table* needs no ALTER — only somebody re-running the schema.

    Nothing prompted that, so a stale local database failed partway through a
    collection with a bare "no such table: scan_progress", naming the table but
    not the remedy. connect() now applies the schema itself.
    """
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE companies(company_id INTEGER PRIMARY KEY, name TEXT, source TEXT);"
    )
    conn.commit()
    conn.close()

    from issuer_data.storage.db import connect

    live = connect(db)
    tables = {r[0] for r in live.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scan_progress" in tables
    assert "api_call_budget" in tables


def test_connect_preserves_existing_data(tmp_path):
    """Auto-applying the schema must never disturb rows already stored."""
    from issuer_data.storage.db import connect, init_db

    db = tmp_path / "live.sqlite"
    init_db(db)
    seed = connect(db)
    seed.execute("INSERT INTO companies(company_id, name, source) VALUES (7,'Kept','dart')")
    seed.commit()
    seed.close()

    again = connect(db)
    assert again.execute("SELECT name FROM companies WHERE company_id=7").fetchone()[0] == "Kept"


def test_ensure_schema_can_be_switched_off(tmp_path):
    """A read-only caller can open a database without altering its structure."""
    from issuer_data.storage.db import connect

    db = tmp_path / "untouched.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE only_this(x INTEGER);")
    conn.commit()
    conn.close()

    live = connect(db, ensure_schema=False)
    tables = {r[0] for r in live.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"only_this"}
