import sqlite3
from importlib import resources

import pytest


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """In-memory SQLite with the package schema applied."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    schema = resources.files("issuer_data.storage").joinpath("schema.sql").read_text(
        encoding="utf-8"
    )
    c.executescript(schema)
    yield c
    c.close()
