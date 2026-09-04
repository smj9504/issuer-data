"""Test database fixtures.

The suite runs against a real PostgreSQL, because the code it tests speaks
PostgreSQL: identity columns, ON CONFLICT targets and string_agg all behave in
ways an in-memory stand-in would only approximate, and the bugs worth catching
here are exactly the ones an approximation hides.

Bring it up with:

    docker compose -f docker-compose.test.yml up -d

Tests skip, rather than fail, when it is not running -- a machine without the
container should still be able to run the parser tests, which are most of them.
"""

from importlib import resources

import psycopg
import pytest

from issuer_data.storage.rows import row_factory

# The container from docker-compose.test.yml. Overridable for a different local
# server, but never point it at a collection database: this truncates freely.
TEST_DSN = "postgresql://issuer:issuer@localhost:55432/issuer_test"


def schema_sql() -> str:
    """The shipped schema, read from the installed package rather than the tree."""
    return resources.files("issuer_data.storage").joinpath("schema.sql").read_text(
        encoding="utf-8"
    )


@pytest.fixture(scope="session")
def _pg_dsn() -> str:
    """Verify the server is reachable and apply the schema once for the session."""
    import os

    dsn = os.environ.get("ISSUER_TEST_DSN", TEST_DSN)
    try:
        with psycopg.connect(dsn, connect_timeout=5, autocommit=True) as conn:
            # An interrupted run (Ctrl-C, a debugger, a killed process) can
            # leave a session holding locks. TRUNCATE then waits on it and the
            # next run hangs with nothing to show for it, so clear the ground
            # before starting rather than inheriting someone else's lock.
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            conn.execute(schema_sql())
            # A test that opens a connection and never closes it leaves a
            # transaction holding locks, and the next TRUNCATE waits on it
            # forever -- a hung suite with no failing test to point at. These
            # turn that into a prompt error naming the statement.
            conn.execute("ALTER DATABASE issuer_test SET lock_timeout = '10s'")
            conn.execute(
                "ALTER DATABASE issuer_test "
                "SET idle_in_transaction_session_timeout = '30s'"
            )
    except psycopg.Error as exc:
        pytest.skip(
            f"PostgreSQL not reachable at {dsn} ({exc.__class__.__name__}). "
            "Start it with: docker compose -f docker-compose.test.yml up -d"
        )
    return dsn


def _truncate(dsn: str) -> None:
    """Empty every table and restart the identity sequences.

    On its own connection, in autocommit, and before the test's connection is
    handed over. TRUNCATE takes an exclusive lock, so it blocks behind any
    other session that is mid-transaction -- and a test connection that has
    run a statement and not committed is exactly that. Doing this first, and
    on a connection that is closed immediately, keeps one test's leftovers
    from hanging the next one.

    RESTART IDENTITY matters beyond tidiness: several tests insert explicit
    ids, which do not advance a sequence, so without the reset a later
    generated id would collide with one written by hand earlier.
    """
    with psycopg.connect(dsn, autocommit=True, row_factory=row_factory) as c:
        rows = c.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        names = ", ".join(f'"{r["tablename"]}"' for r in rows)
        if names:
            c.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")


@pytest.fixture()
def conn(_pg_dsn) -> psycopg.Connection:
    """An empty database with the package schema applied."""
    _truncate(_pg_dsn)
    c = psycopg.connect(_pg_dsn, row_factory=row_factory)
    yield c
    # Roll back whatever the test left open before closing, so a failure
    # part-way through cannot leave a lock behind for the next test. A test
    # that closed the connection on purpose -- the dropped-connection case --
    # has nothing to roll back.
    try:
        c.rollback()
    except psycopg.Error:
        pass
    c.close()


def new_db(_dsn: str = "") -> psycopg.Connection:
    """A connection to an empty database, for tests that build their own.

    Shares the one server rather than creating another database, so it empties
    what is there first; a test using both this and the `conn` fixture would be
    talking to the same rows either way.
    """
    import os

    dsn = _dsn or os.environ.get("ISSUER_TEST_DSN", TEST_DSN)
    _truncate(dsn)
    return psycopg.connect(dsn, row_factory=row_factory)
