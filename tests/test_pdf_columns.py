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


# ------------------------------------------------------- column reading order
def _two_column_pdf(heading: str, left: list[str], right: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=560, height=340)
    page.insert_text((50, 40), heading, fontname=FONT, fontsize=11)
    for i, (l, r) in enumerate(zip(left, right)):
        page.insert_text((50, 90 + i * 24), l, fontname=FONT, fontsize=10)
        page.insert_text((320, 90 + i * 24), r, fontname=FONT, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out


_LEFT = [f"LEFT sentence {i} of the left column." for i in range(5)]
_RIGHT = [f"RIGHT sentence {i} of the right column." for i in range(5)]


def test_columns_are_read_one_after_the_other():
    """pdfplumber groups by y, so the two columns arrive welded line by line."""
    from issuer_data.pdf_columns import column_aware_lines

    content = _two_column_pdf("CHAIRMAN STATEMENT spanning the whole measure", _LEFT, _RIGHT)
    welded = first_page(content).extract_text_lines(strip=True)
    assert any("LEFT" in ln["text"] and "RIGHT" in ln["text"] for ln in welded)

    texts = [ln["text"] for ln in column_aware_lines(first_page(content))]
    assert [t for t in texts if t.startswith("LEFT")] == _LEFT
    assert texts.index("LEFT sentence 4 of the left column.") < texts.index(
        "RIGHT sentence 0 of the right column.")


def test_spanning_heading_stays_above_its_columns():
    from issuer_data.pdf_columns import column_aware_lines

    content = _two_column_pdf("CHAIRMAN STATEMENT spanning the whole measure", _LEFT, _RIGHT)
    texts = [ln["text"] for ln in column_aware_lines(first_page(content))]
    assert texts[0].startswith("CHAIRMAN STATEMENT")


def test_single_column_page_is_left_to_pdfplumber():
    """Returning None keeps ordinary documents on the original line grouping."""
    from issuer_data.pdf_columns import column_aware_lines

    rows = [[(60, f"A single column line number {i} of running prose.")] for i in range(8)]
    assert column_aware_lines(first_page(make_pdf(rows))) is None


def test_a_tables_label_gap_is_not_a_column_gutter():
    """A financial statement has a wide gap too; reordering it tears rows apart."""
    from issuer_data.pdf_columns import column_aware_lines

    assert column_aware_lines(first_page(_financial_pdf())) is None


# ----------------------------------------------------------- rotated text (90°)
# Some filings render org-chart labels and table headers with a 90-degree-
# rotated glyph matrix. pdfplumber's default bottom-to-top reading assumption
# for rotated runs is backwards for these, so both a word's own characters and
# the word order within a run come back mirror-reversed — confirmed against a
# real prospectus, where "Placing:" extracted as ":gnicalP". insert_text(...,
# rotate=90) reproduces the same matrix sign pattern pdfplumber reported on
# that real document, so it is a faithful stand-in here.
def _rotated_pdf(text: str, width: float = 200, height: float = 400) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((100, 350), text, fontname=FONT, fontsize=SIZE, rotate=90)
    out = doc.tobytes()
    doc.close()
    return out


def test_rotated_word_reads_correctly():
    words = first_page(_rotated_pdf("OFFERING")).extract_words(
        keep_blank_chars=False, char_dir_rotated="btt")
    assert [w["text"] for w in words] == ["OFFERING"]


def test_rotated_word_is_mirrored_without_the_fix():
    """Documents the bug this guards against, so a pdfplumber upgrade that
    changes the default is caught here rather than silently in production."""
    words = first_page(_rotated_pdf("OFFERING")).extract_words(keep_blank_chars=False)
    assert [w["text"] for w in words] == ["GNIREFFO"]


def test_rotated_multi_word_run_reads_in_order():
    words = first_page(_rotated_pdf("THE OFFER PRICE")).extract_words(
        keep_blank_chars=False, char_dir_rotated="btt")
    assert [w["text"] for w in words] == ["THE", "OFFER", "PRICE"]


def test_rotated_table_cell_reads_correctly():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    xs, ys = (40, 160, 280), (60, 150)      # a tall cell so "PLACING" fits vertically
    for y in ys:
        page.draw_line(pymupdf.Point(xs[0], y), pymupdf.Point(xs[-1], y))
    for x in xs:
        page.draw_line(pymupdf.Point(x, ys[0]), pymupdf.Point(x, ys[-1]))
    page.insert_text((xs[0] + 14, ys[1] - 4), "PLACING", fontname=FONT,
                     fontsize=SIZE, rotate=90)
    content = doc.tobytes()
    doc.close()

    tables = first_page(content).find_tables()
    assert tables
    rows = tables[0].extract(char_dir_rotated="btt")
    assert rows[0][0] == "PLACING"


def test_upright_tables_unaffected_by_rotation_fix():
    """char_dir_rotated="btt" must be a no-op for text that was never rotated."""
    page = first_page(_financial_pdf())
    default = page.extract_words(keep_blank_chars=False)
    fixed = page.extract_words(keep_blank_chars=False, char_dir_rotated="btt")
    assert default == fixed
    assert find_column_tables(first_page(_financial_pdf())) == \
        find_column_tables(first_page(_financial_pdf()))


def test_extract_structured_reads_rotated_narrative_in_order():
    from issuer_data.pdf_extract import extract_structured

    doc = pymupdf.open()
    page = doc.new_page(width=560, height=400)
    page.insert_text((100, 350), "GLOBAL OFFERING", fontname=FONT,
                     fontsize=SIZE, rotate=90)
    page.insert_text((150, 60), "This announcement relates to the placing.",
                     fontname=FONT, fontsize=SIZE)
    content = doc.tobytes()
    doc.close()

    result = extract_structured(content)
    assert "GLOBAL" in result.text and "OFFERING" in result.text
    assert "GNIREFFO" not in result.text
    assert "LABOLG" not in result.text


# ---------------------------------------------- rotated table STRUCTURE (grid)
# char_dir_rotated fixes reading order for a rotated run of text, but
# find_column_tables' own row/column grouping (_group_rows/_split_cells)
# still assumes top=row, x=column — backwards for a page whose glyph matrix
# is itself rotated 90 degrees. A wide summary table (many columns) is
# sometimes typeset sideways on a portrait page for exactly that reason: what
# was one row of the original table becomes a fixed x0 here, walked in
# reading order as top decreases. Confirmed against the same real prospectus
# page as above (2024123100152.pdf, page 331) before writing this transform.
def _rotated_column_pdf(rows: list[list[str]], width: float = 500,
                        height: float = 500, col_gap: float = 100) -> bytes:
    """Each inner list is one row of the ORIGINAL (unrotated) table, rendered
    as a column of rotated text on the page — mirroring how the real filing
    lays a wide table sideways onto a portrait page.

    Each column sits at a FIXED offset along the rotated run, same as a real
    table's ruled/whitespace-aligned columns: cell N always starts col_gap
    past cell N-1's start, regardless of how long cell N-1's own text was.
    An earlier version of this helper walked past each cell by that cell's
    own rendered width, which only lines a grid up when every row's cells
    happen to be the same length — not a property either a real filing or a
    real test fixture should depend on.
    """
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    row_x_positions = [60 + i * 70 for i in range(len(rows))]
    for x, row in zip(row_x_positions, rows, strict=True):
        for col_i, cell in enumerate(row):
            y = height - 40 - col_i * col_gap
            page.insert_text((x, y), cell, fontname=FONT, fontsize=SIZE, rotate=90)
    out = doc.tobytes()
    doc.close()
    return out


def test_rotated_wide_table_is_reconstructed_with_correct_cell_order():
    original_rows = [
        ["Investor", "Amount", "Shares", "Percent"],
        ["Greenwoods", "20.00", "2792100", "12.86%"],
        ["UBSAMSingapore", "20.00", "2679000", "12.34%"],
        ["FullgoalFund", "7.00", "977100", "4.50%"],
    ]
    content = _rotated_column_pdf(original_rows)
    tables = find_column_tables(first_page(content))
    assert tables, "rotated table should be detected, not silently dropped"

    got_rows = tables[0]["rows"]
    # Header row is optional depending on _header_rows' look-back; the body
    # (numeric data rows) is what must survive intact and in original order.
    body = [r for r in got_rows if any(cell.replace(".", "").isdigit() for cell in r)]
    assert [r[0] for r in body] == ["Greenwoods", "UBSAMSingapore", "FullgoalFund"]
    assert body[0] == ["Greenwoods", "20.00", "2792100", "12.86%"]
    assert body[2] == ["FullgoalFund", "7.00", "977100", "4.50%"]


def test_rotated_table_bbox_and_col_x_are_in_page_coordinates():
    """bbox/col_x must come back in the page's own (unrotated) coordinate
    system — pdf_extract.py and downstream cross-page stitching compare these
    against other tables' bbox/col_x, which are never in a rotated frame."""
    content = _rotated_column_pdf([
        ["Investor", "Amount", "Percent"],
        ["Greenwoods", "20.00", "12.86%"],
        ["UBSAMSingapore", "20.00", "12.34%"],
        ["FullgoalFund", "7.00", "4.50%"],  # >= MIN_ROWS body rows required
    ])
    page = first_page(content)
    tables = find_column_tables(page)
    assert tables
    bbox = tables[0]["bbox"]
    # A page-coordinate bbox must fit within the page; a bbox still in the
    # rotated frame would have swapped, often out-of-range, axes.
    assert 0 <= bbox[0] <= bbox[2] <= page.width
    assert 0 <= bbox[1] <= bbox[3] <= page.height
    assert all(0 <= x <= page.width for x in tables[0]["col_x"])
