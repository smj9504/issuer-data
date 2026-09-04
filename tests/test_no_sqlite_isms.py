"""Cheap greps for dialect mistakes that are expensive to find any other way.

Each of these was a real bug during the move off SQLite, and each is the kind
that either fails far from its cause or does not fail at all.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "issuer_data"
TESTS = Path(__file__).resolve().parent

SQL_KEYWORD = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|VALUES|WHERE|ON\s+CONFLICT)\b",
    re.IGNORECASE,
)


def _python_files():
    """Every source and test file except this one.

    This file quotes the patterns it bans, so it would always match itself.
    """
    here = Path(__file__).resolve()
    for root in (SRC, TESTS):
        for path in root.rglob("*.py"):
            if "__pycache__" not in path.parts and path.resolve() != here:
                yield path


def test_no_multiplied_placeholder_strings():
    """`",".join("%s" * n)` yields `%,s,%,s` -- valid Python, broken SQL.

    The idiom is correct with a single-character placeholder, so it survives
    the rewrite from `?` looking untouched, and what it produces may still
    parse. The list form is the one that works.
    """
    offenders = [
        f"{p.name}:{i}"
        for p in _python_files()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"""["']%s["']\s*\*""", line)
    ]
    assert not offenders, f'use ",".join(["%s"] * n): {offenders}'


def test_no_qmark_placeholders_in_sql():
    """psycopg binds %s; a leftover ? is passed through as a literal.

    The failure surfaces as "0 placeholders but N parameters were passed",
    which names neither the file nor the statement.
    """
    offenders = []
    for path in _python_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "?" not in line or not SQL_KEYWORD.search(line):
                continue
            if "http" in line or "<?xml" in line:   # URLs and XML declarations
                continue
            offenders.append(f"{path.name}:{i}")
    assert not offenders, f"SQL still using ? placeholders: {offenders}"


def test_no_sqlite_only_constructs():
    """Constructs PostgreSQL does not have, or spells differently."""
    banned = {
        "sqlite_master": "information_schema / pg_catalog",
        "PRAGMA ": "server configuration",
        "executescript": "cursor.execute with a multi-statement string",
        "GROUP_CONCAT": "string_agg",
        "INSERT OR REPLACE": "INSERT ... ON CONFLICT DO UPDATE",
        "INSERT OR IGNORE": "INSERT ... ON CONFLICT DO NOTHING",
        ".lastrowid": "INSERT ... RETURNING",
        "printf(": "to_char",
    }
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for needle, replacement in banned.items():
            if needle in text:
                offenders.append(f"{path.name}: {needle!r} -> use {replacement}")
    assert not offenders, offenders


def test_schema_declares_no_four_byte_floats():
    """REAL is 8 bytes in SQLite and 4 in PostgreSQL.

    At 4 bytes a share count or a KRW total loses digits silently -- no error,
    just a reconciliation that no longer comes out to zero.
    """
    schema = (SRC / "storage" / "schema.sql").read_text(encoding="utf-8")
    assert not re.search(r"\bREAL\b", schema), "use DOUBLE PRECISION, not REAL"
