"""Date and fiscal-period normalization helpers."""

from __future__ import annotations

import datetime as _dt


def to_iso(value) -> str | None:
    """Normalize a date-ish value to 'YYYY-MM-DD' or None."""
    if value is None:
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        return None
    # Common compact form YYYYMMDD (DART rcept_dt, etc.)
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    # Truncate ISO datetime to date
    if "T" in s:
        s = s.split("T", 1)[0]
    if " " in s:
        s = s.split(" ", 1)[0]
    return s[:10]


def today_iso() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def years_ago_iso(years: int) -> str:
    d = _dt.date.today()
    try:
        d = d.replace(year=d.year - years)
    except ValueError:  # Feb 29
        d = d.replace(month=2, day=28, year=d.year - years)
    return d.strftime("%Y-%m-%d")


def compact(iso_date: str) -> str:
    """'2024-01-31' -> '20240131' (for pykrx / DART params)."""
    return iso_date.replace("-", "")


def default_range(start: str | None, end: str | None, default_years: int = 1) -> tuple[str, str]:
    """Fill in a (start, end) ISO date range with sensible defaults."""
    end = to_iso(end) or today_iso()
    start = to_iso(start) or years_ago_iso(default_years)
    return start, end
