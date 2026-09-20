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


def test_an_old_peer_key_is_carried_across(_pg_dsn):
    """init-db upgrades a database whose company_peers predates `relation`.

    The SQLite build this replaced could not alter a key, so the answer there
    was a rebuild; Postgres does it in place, and the guard reads the live key's
    columns rather than a version string, so it is a no-op the second time.
    """
    init_db(_pg_dsn)
    with connect(_pg_dsn) as setup:
        setup.execute("TRUNCATE company_peers")
        # Put the table back the way it was before the impact graph.
        setup.execute("ALTER TABLE company_peers DROP CONSTRAINT company_peers_pkey")
        setup.execute("ALTER TABLE company_peers DROP COLUMN direction, "
                      "DROP COLUMN weight, DROP COLUMN evidence")
        setup.execute("ALTER TABLE company_peers ALTER COLUMN relation DROP NOT NULL")
        setup.execute("ALTER TABLE company_peers "
                      "ADD PRIMARY KEY (company_id, peer_company_id, source)")
        setup.execute("INSERT INTO companies(company_id, name, source) "
                      "VALUES (901, 'A', 'fmp'), (902, 'B', 'fmp')")
        # A row from the old world, carrying no relation at all.
        setup.execute("INSERT INTO company_peers(company_id, peer_company_id, source) "
                      "VALUES (901, 902, 'fmp')")
        setup.commit()

    init_db(_pg_dsn)

    with connect(_pg_dsn) as conn:
        key = conn.execute(
            "SELECT a.attname FROM pg_constraint con "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid "
            "AND a.attnum = ANY(con.conkey) "
            "WHERE con.conrelid = 'company_peers'::regclass AND con.contype = 'p' "
            "ORDER BY a.attname"
        ).fetchall()
        assert [r["attname"] for r in key] == [
            "company_id", "peer_company_id", "relation", "source"]
        kept = conn.execute("SELECT relation, weight FROM company_peers").fetchone()
        # The row survives; a key column cannot be NULL, so it says what it is.
        assert (kept["relation"], kept["weight"]) == ("unknown", None)
        # The upgraded table has to be the same shape as a fresh one, not just
        # the same columns: ADD COLUMN IF NOT EXISTS attaches no constraint, so
        # without re-adding them by name the checks would exist only on a
        # database that had never been upgraded.
        checks = conn.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'company_peers'::regclass AND contype = 'c' "
            "ORDER BY conname"
        ).fetchall()
        assert [r["conname"] for r in checks] == [
            "company_peers_direction_check", "company_peers_weight_check"]

    init_db(_pg_dsn)  # the guard must no-op now that the key is the new one
    with connect(_pg_dsn) as conn:
        n = conn.execute("SELECT count(*) AS n FROM company_peers").fetchone()["n"]
        assert n == 1
    with connect(_pg_dsn) as cleanup:
        cleanup.execute("DELETE FROM companies WHERE company_id IN (901, 902)")
        cleanup.commit()
