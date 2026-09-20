"""Cheap greps for dialect mistakes that are expensive to find any other way.

Each of these was a real bug during the move off SQLite, and each is the kind
that either fails far from its cause or does not fail at all.
"""

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "stock_data"
TESTS = Path(__file__).resolve().parent

# A '?' sitting where a bind goes. Reading the position rather than hunting for
# a nearby SELECT/WHERE is what lets this see SQL assembled in fragments: a
# clause appended as " AND s.market=?" carries no keyword for a line-at-a-time
# reader to key on, and that is exactly the shape that kept getting through.
QMARK_BIND = re.compile(
    r"[=<>]\s*\?"                        # market=? · verdict = ? · source <> ?
    r"|[(,]\s*\?"                        # VALUES (? · ,?
    r"|\?\s*[),]"                        # ?) · ?,
    r"|(?:LIMIT|IN|VALUES)\s*\(?\s*\?",  # LIMIT ? · IN (?
    re.IGNORECASE,
)


def _plain_strings(path):
    r"""Every non-raw string literal in a file, as (line, text).

    The raw prefix is the whole trick. A legitimate '?' lives in a regex --
    r"(?:...)", r"\d+?", r"(?P<v>.+?)" -- and a regex is written raw, so
    reading the token's own prefix separates it from a leftover bind without
    having to understand either. Scanning literals rather than lines also keeps
    prose out: a docstring asking "where does this come from?" is not a bind,
    and the old grep flagged one.
    """
    try:
        tokens = tokenize.generate_tokens(
            io.StringIO(path.read_text(encoding="utf-8")).readline)
        for tok in tokens:
            if tok.type != tokenize.STRING:
                continue
            prefix = tok.string[:len(tok.string) - len(tok.string.lstrip("rRbBuUfF"))]
            if "r" in prefix.lower():
                continue
            yield tok.start[0], tok.string[len(prefix):].strip("\"'")
    except tokenize.TokenError:            # a file we cannot read is not a finding
        return


def _qmark_binds(path):
    """Lines holding a ? where psycopg expects %s."""
    return [line for line, text in _plain_strings(path)
            if "?" in text and QMARK_BIND.search(text) and "<?xml" not in text]


def _python_files():
    """Every source and test file except this one.

    This file quotes the patterns it bans, so it would always match itself.
    """
    here = Path(__file__).resolve()
    for root in (SRC, TESTS):
        for path in root.rglob("*.py"):
            if "__pycache__" not in path.parts and path.resolve() != here:
                yield path


MULTIPLIED_PLACEHOLDER = re.compile(r"""["'](?:%s|\?)["']\s*\*""")


def _multiplied_placeholders(path):
    """Lines building a placeholder list by repeating a one-character string."""
    return [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if MULTIPLIED_PLACEHOLDER.search(line)]


def test_no_multiplied_placeholder_strings():
    """`",".join("%s" * n)` yields `%,s,%,s` -- valid Python, broken SQL.

    The idiom is correct with a single-character placeholder, so it survives
    the rewrite from `?` looking untouched, and what it produces may still
    parse. The list form is the one that works.

    `"?" * n` is banned here rather than by the bind check above, because the
    literal it repeats is a bare "?" and nothing about the string says which it
    is -- a display placeholder for an unknown page number is spelled exactly
    the same. What it is being *done with* is the tell, and that is on this
    line, not inside the string.
    """
    offenders = [f"{p.name}:{i}" for p in _python_files()
                 for i in _multiplied_placeholders(p)]
    assert not offenders, f'use ",".join(["%s"] * n): {offenders}'


def test_no_qmark_placeholders_in_sql():
    """psycopg binds %s; a leftover ? is passed through as a literal.

    The failure surfaces as "0 placeholders but N parameters were passed",
    which names neither the file nor the statement.
    """
    offenders = [f"{p.name}:{i}" for p in _python_files() for i in _qmark_binds(p)]
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


# Every one of these is a real line from this repo's history that the keyword
# grep these checks replaced did not flag. They are kept as a fixture rather
# than described, because the point is not that the rule is reasonable -- it is
# that these seven specific shapes stay caught.
SHAPES_THAT_GOT_PAST = (
    'sql += " AND s.market=?"',
    'sql += " AND s.symbol IN (%s)" % ",".join("?" * len(norm))',
    'sql.append("AND verdict = ?")',
    'sql.append("ORDER BY CASE verdict WHEN \'fail\' THEN 0 ELSE 1 END, "\n'
    '           "token_recall ASC, checked_at DESC LIMIT ?")',
    'sql.append("AND fiscal_year = ?")',
    'sql.append("AND source <> ?")',
    'sql.append("ORDER BY fiscal_year DESC, account LIMIT ?")',
)

# Things that are not binds and must stay quiet. The first four are why the
# bare-"?" case is handled by the multiplication check and not by position: as
# literals they are indistinguishable from the placeholder in `"?" * n`.
SHAPES_THAT_ARE_NOT_BINDS = (
    'page = f"p.{value.page}" if value.page else "?"',
    'account = fact.get("account_local") or "?"',
    'label = next((str(c) for c in row[:2] if str(c or "").strip()), "?")',
    'sep = "&" if "?" in dsn else "?"',
    '_URL = "https://api.gleif.org/api/v1/lei-records?filter[x]={n}&page[size]=1"',
    '_PAGE_NUM_RE = re.compile(r"^[\\s]*(?:page|p\\.?)?\\s*\\d+\\s*(?:of\\s*\\d+)?$")',
    'XML_DECL = "<?xml version=\'1.0\'?>"',
    'assert "?" not in text  # prose: where does this come from?',
)


def _flagged(tmp_path, source: str, name: str) -> bool:
    f = tmp_path / f"{name}.py"
    f.write_text(source + "\n", encoding="utf-8")
    return bool(_qmark_binds(f) or _multiplied_placeholders(f))


def test_the_guard_still_catches_what_once_got_past_it(tmp_path):
    missed = [s for i, s in enumerate(SHAPES_THAT_GOT_PAST)
              if not _flagged(tmp_path, s, f"bind{i}")]
    assert not missed, f"these went back to being invisible: {missed}"


def test_the_guard_stays_quiet_on_what_is_not_a_bind(tmp_path):
    """A guard that cries wolf gets muted, and then it guards nothing."""
    noisy = [s for i, s in enumerate(SHAPES_THAT_ARE_NOT_BINDS)
             if _flagged(tmp_path, s, f"quiet{i}")]
    assert not noisy, f"false positives: {noisy}"
