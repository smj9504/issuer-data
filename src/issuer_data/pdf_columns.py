"""Find tables that are aligned with whitespace instead of drawn with rules.

pdfplumber's `find_tables()` looks for ruling lines, which is right for filled-in
forms (HKEX Monthly Returns, Next Day Disclosure Returns) but blind to the way
results announcements and annual reports are typeset: columns held apart by
spacing alone, no borders. Those are the documents whose numbers matter most, and
they were yielding nothing.

Switching pdfplumber to its "text" strategy is not the fix — it shreds running
prose into arbitrary columns, which is worse than finding nothing because the
output still looks like data. So this module reconstructs the grid itself and
only promotes a region to a table when it behaves like one:

  * words are grouped into rows, and each row split into cells at gaps that are
    wide compared to that page's own median word spacing;
  * a run of at least `min_rows` consecutive rows must share a column grid,
    matched on either cell edge — headers sit right-aligned over the numbers
    they label, so a left-edge-only comparison misses them;
  * the run must be mostly numeric beyond the first column, which is what
    separates a financial table from justified prose that happens to line up.

Confidence is the body's grid consistency, so a ragged detection scores low and
flows into the existing review/escalation path rather than passing as clean data.
"""

from __future__ import annotations

import re
import statistics
from itertools import pairwise

_NUMERIC_CELL = re.compile(r"\(?-?[\d][\d,.\s]*\)?%?")

MIN_ROWS = 3
MIN_COLS = 2
X_TOL = 8.0
NUMERIC_ROWS_FRAC = 0.5
HEADER_LOOK_BACK = 4
GAP_CHARS = 2.2      # a column gap is this many characters wide; a word space is ~0.6


def find_column_tables(page, *, min_rows: int = MIN_ROWS, min_cols: int = MIN_COLS,
                       x_tol: float = X_TOL,
                       numeric_rows_frac: float = NUMERIC_ROWS_FRAC) -> list[dict]:
    """Whitespace-aligned tables on `page`, in `_parse_pages`' table shape."""
    words = page.extract_words(keep_blank_chars=False)
    if len(words) < 10:
        return []

    heights = [w["bottom"] - w["top"] for w in words]
    y_tol = max(1.5, statistics.median(heights) * 0.5)
    # Size the column gap from the font, not from the page's own spacing: on a
    # page that is mostly table, most word gaps ARE column gaps, so a median-of-
    # gaps threshold inflates until nothing splits — which is precisely how a
    # full-page balance sheet would go undetected.
    gap = max(5.0, _char_width(words) * GAP_CHARS)

    rows = [_split_cells(r, gap) for r in _group_rows(words, y_tol) if r]
    edges = [[(c[0], c[1]) for c in row] for row in rows]

    out: list[dict] = []
    consumed: set[int] = set()
    for start, stop in _grid_runs(rows, edges, min_rows, min_cols, x_tol):
        body = rows[start:stop]
        numeric = sum(1 for r in body if any(_is_numeric(c[4]) for c in r[1:]))
        if numeric / len(body) < numeric_rows_frac:
            continue  # prose that happens to line up, not a table
        head = _header_rows(edges, start, x_tol, min_cols, consumed)
        consumed.update(head)
        consumed.update(range(start, stop))

        table = [rows[i] for i in head] + body
        # Grid consistency of the body only: header rows legitimately carry one
        # cell fewer (no label column), and counting them would punish a table
        # that is in fact perfectly regular.
        modal = statistics.mode([len(r) for r in body])
        out.append({
            "bbox": (min(c[0] for r in table for c in r),
                     min(c[2] for r in table for c in r),
                     max(c[1] for r in table for c in r),
                     max(c[3] for r in table for c in r)),
            "col_x": [c[0] for c in table[0]],
            "rows": [[c[4] for c in r] for r in table],
            "source_engine": "column-geometry",
            "structure_confidence": sum(1 for r in body if len(r) == modal) / len(body),
        })
    return out


def _char_width(words: list[dict]) -> float:
    widths = [(w["x1"] - w["x0"]) / len(w["text"]) for w in words if w["text"].strip()]
    return statistics.median(widths) if widths else 3.0


# A cell is (x0, x1, top, bottom, text).
def _group_rows(words: list[dict], y_tol: float) -> list[list[dict]]:
    rows: list[list[dict]] = []
    current: list[dict] = []
    baseline: float | None = None
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if baseline is None or abs(w["top"] - baseline) <= y_tol:
            current.append(w)
            baseline = w["top"] if baseline is None else baseline
        else:
            rows.append(sorted(current, key=lambda w: w["x0"]))
            current, baseline = [w], w["top"]
    if current:
        rows.append(sorted(current, key=lambda w: w["x0"]))
    return rows


def _split_cells(row: list[dict], gap: float) -> list[tuple]:
    groups: list[list[dict]] = []
    buf = [row[0]]
    for prev, w in pairwise(row):
        if w["x0"] - prev["x1"] > gap:
            groups.append(buf)
            buf = [w]
        else:
            buf.append(w)
    groups.append(buf)
    return [(g[0]["x0"], g[-1]["x1"], min(w["top"] for w in g), max(w["bottom"] for w in g),
             " ".join(w["text"] for w in g)) for g in groups]


def _is_numeric(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped and _NUMERIC_CELL.fullmatch(stripped)
                and any(ch.isdigit() for ch in stripped))


def _aligned(a: list[tuple], b: list[tuple], tol: float) -> bool:
    """Whether two rows share a column grid, compared on either cell edge.

    A header like "31 March" is right-aligned over the numbers beneath it, so its
    left edge misses by more than any sane tolerance while its right edge lands.
    """
    if not a or not b:
        return False
    hits = sum(1 for x0, x1 in b
               if any(abs(x0 - y0) <= tol or abs(x1 - y1) <= tol for y0, y1 in a))
    return hits >= max(2, min(len(a), len(b)) - 1)


def _grid_runs(rows: list, edges: list, min_rows: int, min_cols: int,
               tol: float) -> list[tuple[int, int]]:
    """Half-open index ranges of consecutive rows that share a column grid."""
    runs: list[tuple[int, int]] = []
    length, first = 0, 0
    for idx, cells in enumerate(rows):
        too_narrow = len(cells) < min_cols
        if too_narrow or (length and not _aligned(edges[idx - 1], edges[idx], tol)):
            if length >= min_rows:
                runs.append((first, idx))
            length = 0
            if too_narrow:
                continue
        if not length:
            first = idx
        length += 1
    if length >= min_rows:
        runs.append((first, len(rows)))
    return runs


def _header_rows(edges: list, start: int, tol: float, min_cols: int,
                 consumed: set[int], look_back: int = HEADER_LOOK_BACK) -> list[int]:
    """Indices of the column-label rows above a numeric run.

    Rows too narrow to be part of the grid — a units note such as "(RMB in
    millions, unless specified)" — are stepped over rather than absorbed. The
    first multi-cell row that does not line up ends the search, and rows already
    taken by an earlier table are never stolen from it.
    """
    head: list[int] = []
    for idx in range(start - 1, max(-1, start - 1 - look_back), -1):
        if idx in consumed:
            break
        if len(edges[idx]) < min_cols:
            continue
        if not _aligned(edges[start], edges[idx], tol):
            break
        head.insert(0, idx)
    return head
