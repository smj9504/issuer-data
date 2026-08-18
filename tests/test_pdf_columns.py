"""Whitespace-aligned (borderless) table detection.

The gap these cover: HKEX results announcements typeset their financial tables
with spacing instead of ruling lines, so pdfplumber's line-based `find_tables()`
returned nothing for exactly the documents whose numbers matter most.
"""

import pytest

from issuer_data.pdf_columns import find_column_tables
from issuer_data.pdf_extract import extract_structured

pymupdf = pytest.importorskip("pymupdf")
pdfplumber = pytest.importorskip("pdfplumber")

FONT, SIZE = "helv", 10


def _width(text: str) -> float:
    return pymupdf.get_text_length(text, fontname=FONT, fontsize=SIZE)


def make_pdf(rows: list[list[tuple]], width: float = 560, height: float = 400) -> bytes:
    """Render rows of (x, text[, "r"]) placements; "r" right-aligns at x."""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    for i, row in enumerate(rows):
        y = 60 + i * 20
        for item in row:
            x, text = item[0], item[1]
            if len(item) > 2 and item[2] == "r":
                x -= _width(text)
            page.insert_text((x, y), text, fontname=FONT, fontsize=SIZE)
    out = doc.tobytes()
    doc.close()
    return out


def first_page(content: bytes):
    import io
    return pdfplumber.open(io.BytesIO(content)).pages[0]


# Numbers right-aligned at these column edges, the way a real filing sets them.
COLS = (260, 350, 440, 530)


def _financial_pdf(extra: list[list[tuple]] | None = None) -> bytes:
    rows = [
        [(COLS[0], "31 March", "r"), (COLS[1], "31 March", "r"),
         (COLS[2], "31 December", "r"), (COLS[3], "on-quarter", "r")],
        [(COLS[0], "2026", "r"), (COLS[1], "2025", "r"),
         (COLS[2], "2025", "r"), (COLS[3], "change", "r")],
        [(60, "(RMB in millions, unless specified)")],
        [(60, "Revenues"), (COLS[0], "196,458", "r"), (COLS[1], "180,022", "r"),
         (COLS[2], "194,371", "r"), (COLS[3], "1%", "r")],
        [(60, "Gross profit"), (COLS[0], "111,265", "r"), (COLS[1], "100,493", "r"),
         (COLS[2], "108,289", "r"), (COLS[3], "3%", "r")],
        [(60, "Operating profit"), (COLS[0], "67,375", "r"), (COLS[1], "57,566", "r"),
         (COLS[2], "60,338", "r"), (COLS[3], "12%", "r")],
        [(60, "Profit for the period"), (COLS[0], "59,392", "r"), (COLS[1], "49,725", "r"),
         (COLS[2], "59,089", "r"), (COLS[3], "0.5%", "r")],
    ]
    return make_pdf(rows + (extra or []))


def test_borderless_table_is_found_at_all():
    content = _financial_pdf()
    assert first_page(content).find_tables() == []      # the ruled pass sees nothing
    tables = find_column_tables(first_page(content))
    assert len(tables) == 1


def test_borderless_rows_keep_their_cells_separate():
    """Plain text glues adjacent cells together; the point of this is to not."""
    table = find_column_tables(first_page(_financial_pdf()))[0]
    assert ["Revenues", "196,458", "180,022", "194,371", "1%"] in table["rows"]
    assert ["Gross profit", "111,265", "100,493", "108,289", "3%"] in table["rows"]


def test_right_aligned_headers_are_attached():
    """Headers sit right-aligned over their numbers, so left edges never match."""
    rows = find_column_tables(first_page(_financial_pdf()))[0]["rows"]
    assert ["31 March", "31 March", "31 December", "on-quarter"] in rows
    assert ["2026", "2025", "2025", "change"] in rows


def test_units_note_between_header_and_body_is_not_absorbed():
    for row in find_column_tables(first_page(_financial_pdf()))[0]["rows"]:
        assert not any("RMB in millions" in cell for cell in row)


def test_regular_table_is_fully_confident():
    assert find_column_tables(first_page(_financial_pdf()))[0]["structure_confidence"] == 1.0


def test_ragged_grid_scores_below_one():
    """A detection that does not hold its shape must not pass as clean data."""
    ragged = [[(60, "Other income"), (COLS[0], "1,234", "r")]]      # 2 cells, not 5
    table = find_column_tables(first_page(_financial_pdf(ragged)))[0]
    assert table["structure_confidence"] < 1.0


def test_prose_is_not_promoted_to_a_table():
    """pdfplumber's "text" strategy shreds prose into columns; this must not."""
    prose = [
        [(60, "Hong Kong Exchanges and Clearing Limited and The Stock Exchange")],
        [(60, "of Hong Kong Limited take no responsibility for the contents of")],
        [(60, "this announcement, make no representation as to its accuracy and")],
        [(60, "expressly disclaim any liability whatsoever for any loss however")],
        [(60, "arising from or in reliance upon the whole or any part hereof.")],
    ]
    assert find_column_tables(first_page(make_pdf(prose))) == []


def test_aligned_but_non_numeric_columns_are_not_a_table():
    """Alignment alone is not enough — a table of contents lines up too."""
    toc = [
        [(60, "Chairman statement"), (400, "Section one")],
        [(60, "Business review"), (400, "Section two")],
        [(60, "Corporate governance"), (400, "Section three")],
        [(60, "Directors report"), (400, "Section four")],
    ]
    assert find_column_tables(first_page(make_pdf(toc))) == []


def test_short_run_is_not_a_table():
    two_rows = [
        [(60, "Revenues"), (COLS[0], "196,458", "r")],
        [(60, "Gross profit"), (COLS[0], "111,265", "r")],
    ]
    assert find_column_tables(first_page(make_pdf(two_rows))) == []


# ------------------------------------------------------- pipeline integration
def test_structured_pass_reports_the_engine_that_found_it():
    doc = extract_structured(_financial_pdf())
    assert len(doc.tables) == 1
    assert doc.tables[0].source_engine == "column-geometry"
    assert doc.tables[0].confidence == 1.0
    assert not doc.tables[0].needs_review


def test_ragged_detection_is_flagged_for_review():
    ragged = [[(60, "Other income"), (COLS[0], "1,234", "r")]]
    doc = extract_structured(_financial_pdf(ragged), threshold=0.9)
    assert doc.tables[0].confidence < 0.9
    assert doc.tables[0].needs_review


def test_ruled_tables_still_come_from_pdfplumber():
    """The geometry pass runs only where the ruled pass found nothing."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    xs, ys = (40, 160, 280, 360), (60, 90, 120, 150)
    for y in ys:
        page.draw_line(pymupdf.Point(xs[0], y), pymupdf.Point(xs[-1], y))
    for x in xs:
        page.draw_line(pymupdf.Point(x, ys[0]), pymupdf.Point(x, ys[-1]))
    for r, label in enumerate(("Revenues", "Gross profit", "Operating profit")):
        page.insert_text((xs[0] + 4, ys[r] + 18), label, fontname=FONT, fontsize=SIZE)
        for col, value in enumerate(("196,458", "180,022")):
            page.insert_text((xs[col + 1] + 4, ys[r] + 18), value, fontname=FONT, fontsize=SIZE)
    content = doc.tobytes()
    doc.close()

    assert first_page(content).find_tables()          # the ruled pass does see it
    result = extract_structured(content)
    assert result.tables
    assert {t.source_engine for t in result.tables} == {"pdfplumber"}
