"""A row that answers to both a column name and a position.

psycopg's stock factories make you choose: `dict_row` gives `row["close"]` and
raises on `row[0]`, `tuple_row` the reverse. Call sites here want both --
`row["company_id"]` reads better where the column matters, while `fetchone()[0]`
is the natural way to take a lone `COUNT(*)` -- and the database this replaced
allowed both, so the choice would otherwise be a mass edit of call sites for no
behavioural gain.

Slicing and unpacking work as they do on a tuple, so a row still compares equal
to the plain tuple a test writes out by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg import Cursor


class Row(tuple):
    """A tuple that also resolves column names."""

    __slots__ = ()
    _fields: tuple[str, ...] = ()

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._fields.index(key))
            except ValueError:
                raise KeyError(key) from None
        return tuple.__getitem__(self, key)

    def __getattr__(self, name: str) -> Any:
        try:
            return tuple.__getitem__(self, self._fields.index(name))
        except ValueError:
            raise AttributeError(name) from None

    # Mapping-ish helpers, so a row can be handed to dict() or inspected in a
    # log line without the caller knowing which factory produced it.
    def keys(self) -> tuple[str, ...]:
        return self._fields

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, str):
            return key in self._fields
        return tuple.__contains__(self, key)

    def __repr__(self) -> str:
        return "Row(" + ", ".join(
            f"{f}={v!r}" for f, v in zip(self._fields, self)
        ) + ")"


def row_factory(cursor: Cursor) -> Any:
    """psycopg row factory producing `Row` instances.

    The field names come from the cursor description, so a subclass is built
    once per result shape rather than per row.
    """
    desc = cursor.description
    fields = tuple(d.name for d in desc) if desc else ()

    class _Row(Row):
        __slots__ = ()
        _fields = fields

    def make(values: Sequence[Any]) -> _Row:
        return _Row(values)

    return make
