"""PostgreSQL connection factory and schema application."""

from __future__ import annotations

from importlib import resources

import psycopg

from ..config import get_settings
from ..logging import get_logger
from .rows import row_factory

log = get_logger(__name__)

# Bumped whenever schema.sql changes in a way an existing database will not
# have. Nothing here migrates on its own (see `connect`); the check exists so a
# database that missed init-db says so up front instead of failing partway
# through a collection on a table it has never heard of.
SCHEMA_VERSION = "2026-09-04"


def _read_schema() -> str:
    """Load the canonical schema.sql shipped with the package."""
    return resources.files("issuer_data.storage").joinpath("schema.sql").read_text(
        encoding="utf-8"
    )


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def _with_sslmode(dsn: str) -> str:
    """Default an unqualified DSN to a verified TLS connection.

    libpq's own default is `prefer`, which falls back to plaintext without
    saying so. For a database reached over the open internet that is the wrong
    way to fail, so a DSN that does not choose gets `verify-full`; one that
    does is left alone, including a deliberate `disable`.

    A loopback host is exempt: the test container speaks no TLS, and traffic
    that never leaves the machine has nothing to intercept.
    """
    try:
        info = psycopg.conninfo.conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - let psycopg report a malformed DSN
        return dsn
    if "sslmode" in info or str(info.get("host", "")) in _LOCAL_HOSTS:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}sslmode=verify-full"


def redact(dsn: str) -> str:
    """A DSN safe to log: everything but the password."""
    try:
        info = psycopg.conninfo.conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - a malformed DSN must not break logging
        return "<dsn>"
    info.pop("password", None)
    host = info.get("host", "?")
    port = info.get("port", "?")
    return f"{info.get('user', '?')}@{host}:{port}/{info.get('dbname', '?')}"


def connect(dsn: str | None = None, *, ensure_schema: bool = False
            ) -> psycopg.Connection:
    """Open a connection.

    Unlike the file-backed database this replaces, `ensure_schema` defaults to
    False and only checks. Applying the schema on every connect used to be free
    because the database was one local file with one writer; against a shared
    server it means every process that opens a connection issues DROP VIEW and
    CREATE VIEW, and two doing that at once will either deadlock or briefly
    expose a missing view to the other. `init_db` is the one place that writes
    structure.

    What is kept is the diagnosis: a database that predates the current schema
    is reported here, by name, rather than surfacing later as a missing column
    partway through a collection.
    """
    if dsn is None:
        dsn = get_settings().db_dsn
    conn = psycopg.connect(_with_sslmode(dsn), row_factory=row_factory,
                           client_encoding="UTF8", **_KEEPALIVE)
    if ensure_schema:
        _check_schema(conn)
    return conn


# A market-wide sweep holds one connection open for hours while most of its time
# goes on API calls, not queries. Left to the defaults, a NAT or load balancer
# between here and a hosted database drops a connection it has seen no traffic
# on, and the next statement fails hours in. These make the client send its own
# traffic well before that: idle 60s, then probe, and give up after ~2 minutes
# so a genuinely dead connection is reported rather than hung on.
_KEEPALIVE = {
    "keepalives": 1,
    "keepalives_idle": 60,
    "keepalives_interval": 15,
    "keepalives_count": 5,
}


def _check_schema(conn: psycopg.Connection) -> None:
    """Warn when the database's structure is not the one this code expects."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        log.warning("No schema found in %s; run init-db", redact(conn.info.dsn))
        return
    if row is None or row["value"] != SCHEMA_VERSION:
        found = row["value"] if row else "none"
        log.warning(
            "Database schema is %s but this build expects %s; run init-db to "
            "bring it up to date.", found, SCHEMA_VERSION,
        )


def init_db(dsn: str | None = None) -> str:
    """Apply the schema (idempotent). The only path that writes structure."""
    if dsn is None:
        dsn = get_settings().db_dsn
    with psycopg.connect(_with_sslmode(dsn), row_factory=row_factory,
                         client_encoding="UTF8", autocommit=True) as conn:
        conn.execute(_read_schema())
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (SCHEMA_VERSION,),
        )
    log.info("Initialized database at %s (schema %s)", redact(dsn), SCHEMA_VERSION)
    return dsn
