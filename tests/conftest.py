import sqlite3
from importlib import resources

import pytest


def schema_sql() -> str:
    """The shipped schema, read from the installed package rather than the tree."""
    return resources.files("issuer_data.storage").joinpath("schema.sql").read_text(
        encoding="utf-8"
    )


def apply_schema(c: sqlite3.Connection) -> None:
    """Bring a fresh connection up to the shipped schema."""
    c.executescript(schema_sql())


def new_db() -> sqlite3.Connection:
    """A connection to an empty database with the schema applied.

    Tests that need a second, independent database (or that build one mid-test)
    go through here rather than reaching for the driver, so the backend is named
    in exactly one place.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    apply_schema(c)
    return c


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """An empty database with the package schema applied."""
    c = new_db()
    yield c
    c.close()
