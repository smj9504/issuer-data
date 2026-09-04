"""Applying the schema, and reporting a database that has not had it applied.

This replaces the migration tests. Those covered machinery that only existed
because SQLite cannot alter a primary key and will not add a column to an
existing table: the schema was reapplied on every connect, and tables were
rebuilt behind the scenes to widen a key. None of that survives here --
init-db is the only thing that writes structure -- so what is worth pinning is
that applying it twice is safe, that opening a connection does not quietly
change anything, and that a database left behind says so.
"""

import psycopg
import pytest
from conftest import schema_sql

from issuer_data.storage.db import SCHEMA_VERSION, connect, init_db, redact


def test_init_db_is_idempotent(_pg_dsn):
    """Re-running init-db is the documented repair, so it cannot be one-shot."""
    init_db(_pg_dsn)
    init_db(_pg_dsn)
    with connect(_pg_dsn) as conn:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert version["value"] == SCHEMA_VERSION
        views = conn.execute(
            "SELECT count(*) AS n FROM information_schema.views "
            "WHERE table_schema = 'public'"
        ).fetchone()
        assert views["n"] == 6, "a view was dropped and not recreated"


def test_connecting_does_not_write_structure(_pg_dsn):
    """Every process opening a connection must not be issuing DDL.

    Against a shared server that would have them dropping and recreating each
    other's views mid-collection.
    """
    init_db(_pg_dsn)
    with connect(_pg_dsn) as setup:
        setup.execute("DROP VIEW IF EXISTS v_latest_price")
        setup.commit()

    with connect(_pg_dsn, ensure_schema=True) as conn:
        still_missing = conn.execute(
            "SELECT count(*) AS n FROM information_schema.views "
            "WHERE table_schema = 'public' AND table_name = 'v_latest_price'"
        ).fetchone()
        assert still_missing["n"] == 0, "connect() recreated a view"
    init_db(_pg_dsn)  # put it back for whatever runs next


def test_a_stale_database_says_so(_pg_dsn, caplog):
    """The warning is what replaced auto-migration; it has to name the remedy."""
    init_db(_pg_dsn)
    with connect(_pg_dsn) as setup:
        setup.execute(
            "UPDATE schema_meta SET value = '1999-01-01' WHERE key = 'schema_version'"
        )
        setup.commit()

    with caplog.at_level("WARNING"), connect(_pg_dsn, ensure_schema=True) as conn:
        assert conn.execute("SELECT 1 AS n").fetchone()["n"] == 1
    assert "1999-01-01" in caplog.text
    assert "init-db" in caplog.text
    init_db(_pg_dsn)


def test_schema_has_no_four_byte_floats(_pg_dsn):
    """`real` would round a share count or a KRW total; these need `double`.

    1132477 * 285000 has to reconcile exactly against a stored 322755945000,
    and at 4 bytes it does not.
    """
    init_db(_pg_dsn)
    with connect(_pg_dsn) as conn:
        offenders = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type = 'real'"
        ).fetchall()
    assert offenders == [], f"4-byte float columns: {offenders}"


def test_key_text_columns_are_not_dates(_pg_dsn):
    """These read as dates but cannot be date columns.

    The scan cursor stores '' for "no range", and one comparison synthesises a
    date from a fiscal year. A date column rejects both.
    """
    init_db(_pg_dsn)
    with connect(_pg_dsn) as conn:
        for table, column in (("scan_progress", "range_start"),
                              ("scan_progress", "range_end"),
                              ("companies", "updated_at")):
            got = conn.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "AND column_name = %s",
                (table, column),
            ).fetchone()
            assert got["data_type"] == "text", f"{table}.{column} is {got['data_type']}"


def test_schema_applies_to_an_empty_database(_pg_dsn):
    """The whole script, on nothing, in one go -- what a new machine does."""
    with psycopg.connect(_pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.execute(schema_sql())
        n = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchone()[0]
        assert n > 20
    init_db(_pg_dsn)


@pytest.mark.parametrize("dsn,expected", [
    ("postgresql://u:p@db.example.com/x", "sslmode=verify-full"),
    ("postgresql://u:p@db.example.com/x?sslmode=require", "sslmode=require"),
])
def test_a_remote_dsn_gets_a_verified_connection(dsn, expected):
    """libpq's default falls back to plaintext without saying so."""
    from issuer_data.storage.db import _with_sslmode

    assert expected in _with_sslmode(dsn)


def test_the_password_never_reaches_a_log_line():
    assert "hunter2" not in redact("postgresql://u:hunter2@db.example.com:5432/x")
