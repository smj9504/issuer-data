"""`check-db` against a database that missed init-db.

Running this command on a schema-less database is not an error case -- it is
the case the command exists for. It used to crash with a raw psycopg traceback
on the first `schema_meta` read, which both hid the diagnosis it had already
printed and skipped the checks after it.
"""

from __future__ import annotations

import psycopg
import pytest
from conftest import TEST_DSN

from issuer_data.cli import cmd_check_db
from issuer_data.storage.db import SCHEMA_VERSION

SCRATCH = "checkdb_scratch"


def _server_dsn() -> str:
    """The test DSN pointed at `postgres`, so the scratch db can be created."""
    import os

    dsn = os.environ.get("ISSUER_TEST_DSN", TEST_DSN)
    return dsn.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture()
def bare_dsn(_pg_dsn):
    """An empty database with no schema at all -- not even schema_meta."""
    admin = psycopg.connect(_server_dsn(), autocommit=True)
    try:
        admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
        admin.execute(f"CREATE DATABASE {SCRATCH}")
    except psycopg.Error:
        admin.close()
        pytest.skip("cannot create a scratch database on this server")
    admin.close()

    yield _pg_dsn.rsplit("/", 1)[0] + f"/{SCRATCH}"

    admin = psycopg.connect(_server_dsn(), autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
    admin.close()


def _run(monkeypatch, dsn: str, capsys) -> tuple[int, str]:
    """Point `get_settings()` at `dsn` and run the command.

    `get_settings` caches into a module global, so the DSN is swapped by
    replacing that object; monkeypatch restores it however the test ends.
    """
    from issuer_data import config

    monkeypatch.setattr(config, "_settings",
                        config.Settings(_env_file=None, db_dsn=dsn))
    rc = cmd_check_db(object())
    return rc, capsys.readouterr().out


def test_missing_schema_reports_instead_of_raising(bare_dsn, monkeypatch, capsys):
    """No traceback, a named diagnosis, and a non-zero exit."""
    rc, out = _run(monkeypatch, bare_dsn, capsys)

    assert rc == 1, "a database without a schema is not ready for a sweep"
    assert "MISSING -- run init-db" in out
    assert "NOT READY" in out


def test_missing_schema_still_runs_the_later_checks(bare_dsn, monkeypatch, capsys):
    """The aborted transaction must not swallow the checks after it.

    The failed `schema_meta` read poisons the transaction, so without a rollback
    every later query dies with InFailedSqlTransaction and the report loses the
    connection, role and data lines it had not printed yet.
    """
    _rc, out = _run(monkeypatch, bare_dsn, capsys)

    assert "Server     PostgreSQL" in out
    assert "Role " in out and "writes:" in out
    assert "Data       -- (no tables yet)" in out


def test_healthy_database_passes(_pg_dsn, monkeypatch, capsys):
    """The normal path keeps reporting a schema and exiting 0."""
    # The `conn` fixture truncates every table between tests, schema_meta
    # included, so the version row is only there if this test puts it there --
    # otherwise the assertion passes or fails on test ordering.
    with psycopg.connect(_pg_dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (SCHEMA_VERSION,),
        )

    rc, out = _run(monkeypatch, _pg_dsn, capsys)

    assert rc == 0
    assert "MISSING" not in out and "NOT READY" not in out
