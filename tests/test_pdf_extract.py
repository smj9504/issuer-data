"""Tests for the structured PDF engine: stitching, reflow, grounding, storage.

The geometry logic is exercised with synthetic page representations (no real PDF
needed); one reportlab-gated integration test runs the full engine on a real
multi-page PDF whose table is split across the page break.
"""

import pytest

from issuer_data.models import Company, Filing, Security
from issuer_data.pdf_extract import (
    StitchedTable,
    _too_empty,
    ground_numbers,
    reflow_narrative,
    stitch_tables,
)
from issuer_data.storage.repository import Repository


# ------------------------------------------------------------- table stitching
def _page(page_no, height, tables, lines=None):
    return {"page_no": page_no, "width": 600.0, "height": height,
            "lines": lines or [], "tables": tables}


def test_stitch_joins_table_across_page_break():
    header = ["Account", "2023", "2022"]
    p1 = _page(1, 800.0, [{
        "bbox": (50, 600, 550, 780),        # ends near the bottom (780/800)
        "col_x": [50, 250, 400],
        "rows": [header, ["Revenue", "100", "90"]],
    }])
    p2 = _page(2, 800.0, [{
        "bbox": (50, 40, 550, 300),         # starts near the top (40/800)
        "col_x": [51, 249, 401],            # same signature within tolerance
        "rows": [header, ["Cost", "60", "55"]],   # repeated header row
    }])
    tables = stitch_tables([p1, p2])
    assert len(tables) == 1
    t = tables[0]
    assert t.page_start == 1 and t.page_end == 2
    # header appears once; both body rows present (continuation header dropped)
    assert t.rows == [header, ["Revenue", "100", "90"], ["Cost", "60", "55"]]


def test_no_stitch_when_columns_differ():
    p1 = _page(1, 800.0, [{
        "bbox": (50, 600, 550, 780), "col_x": [50, 250, 400],
        "rows": [["A", "1", "2"]],
    }])
    p2 = _page(2, 800.0, [{
        "bbox": (50, 40, 550, 300), "col_x": [50, 300],   # different shape
        "rows": [["B", "3"]],
    }])
    assert len(stitch_tables([p1, p2])) == 2


def test_no_stitch_when_first_table_not_at_bottom():
    p1 = _page(1, 800.0, [{
        "bbox": (50, 100, 550, 300), "col_x": [50, 250, 400],  # middle of page
        "rows": [["A", "1", "2"]],
    }])
    p2 = _page(2, 800.0, [{
        "bbox": (50, 40, 550, 300), "col_x": [50, 250, 400],
        "rows": [["B", "3", "4"]],
    }])
    assert len(stitch_tables([p1, p2])) == 2


# --------------------------------------------------------------- reflow / prose
def _line(text, top):
    return {"text": text, "x0": 50.0, "x1": 550.0, "top": top, "bottom": top + 12}


def test_reflow_strips_headers_footers_and_merges_paragraphs():
    pages = []
    body = [
        "The company reported strong growth in the",   # continues...
        "fiscal year ending December 2023.",           # ...ends sentence
    ]
    for i in range(1, 4):
        lines = [
            _line("ACME CORPORATION ANNUAL REPORT", 20),   # running header
            _line(body[0], 400),
            _line(body[1], 420),
            _line(f"- {i} -", 780),                        # page number
        ]
        pages.append({"page_no": i, "width": 600.0, "height": 800.0, "lines": lines})
    text = reflow_narrative(pages)
    assert "ANNUAL REPORT" not in text          # running header removed
    assert "- 1 -" not in text and "- 2 -" not in text  # page numbers removed
    # the split sentence is merged into one continuous line
    assert "growth in the fiscal year ending December 2023." in text


def test_reflow_chinese_joins_without_a_space():
    """Chinese is written without spaces between words, so a line break closes up."""
    pages = [{"page_no": 1, "width": 600.0, "height": 800.0, "lines": [
        _line("本期純利較", 400),
        _line("去年同期增長。", 420),
    ]}]
    assert "本期純利較去年同期增長。" in reflow_narrative(pages)


def test_reflow_korean_keeps_the_space():
    """Hangul sits in the CJK block but Korean is space-delimited between 어절.

    Joining it like Chinese ran the words together — "300조원을달성하였으며" — in
    every Korean paragraph that wrapped a line.
    """
    pages = [{"page_no": 1, "width": 600.0, "height": 800.0, "lines": [
        _line("당기순이익은", 400),
        _line("전년대비 증가하였다.", 420),
    ]}]
    text = reflow_narrative(pages)
    assert "당기순이익은 전년대비 증가하였다." in text
    assert "당기순이익은전년대비" not in text


def test_reflow_mixed_korean_and_latin_keeps_the_space():
    pages = [{"page_no": 1, "width": 600.0, "height": 800.0, "lines": [
        _line("매출액은 300조원을", 400),
        _line("달성하였다.", 420),
    ]}]
    assert "300조원을 달성하였다." in reflow_narrative(pages)


# ------------------------------------------------------------ numeric grounding
def test_ground_numbers():
    raw = "Revenue 1,234 Cost 567 Total 1801"
    good = [["Revenue", "1,234"], ["Cost", "567"]]
    assert ground_numbers(good, raw) == 1.0
    # a fabricated figure not present in the raw text lowers confidence
    bad = [["Revenue", "1,234"], ["Phantom", "9,999"]]
    assert ground_numbers(bad, raw) == 0.5
    # a table with no multi-digit numbers is trivially grounded
    assert ground_numbers([["Name", "A"]], raw) == 1.0


# ------------------------------------------------------------------ persistence
def test_upsert_filing_tables_idempotent(conn):
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="ACME", cik="1", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="ACME", source="edgar"), cid)
    repo.upsert_filings(cid, [Filing(symbol="ACME", market="US", filing_id="f1",
                                     source="edgar")])
    tables = [StitchedTable(rows=[["A", "B"], ["1", "2"]], page_start=1, page_end=2,
                            confidence=0.9, source_engine="pdfplumber")]
    n1 = repo.upsert_filing_tables(cid, "f1", "edgar", 0, tables)
    repo.commit()
    assert n1 == 4  # 2 rows x 2 cols
    # re-run replaces, does not duplicate
    repo.upsert_filing_tables(cid, "f1", "edgar", 0, tables)
    repo.commit()
    assert conn.execute("SELECT COUNT(*) FROM filing_tables").fetchone()[0] == 4
    span = conn.execute("SELECT DISTINCT page_start, page_end FROM filing_tables").fetchone()
    assert (span[0], span[1]) == (1, 2)


# ------------------------------------------------- integration on a real PDF
def test_extract_structured_on_real_split_table():
    reportlab = pytest.importorskip("reportlab")  # noqa: F841
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import LongTable, SimpleDocTemplate, TableStyle

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=36, bottomMargin=36)
    data = [["Account", "2023", "2022"]]
    for i in range(60):  # enough rows to force a page break
        data.append([f"Item {i}", str(1000 + i), str(900 + i)])
    tbl = LongTable(data, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
    ]))
    doc.build([tbl])
    content = buf.getvalue()

    from issuer_data.pdf_extract import extract_structured
    sdoc = extract_structured(content)
    assert sdoc.page_count >= 2
    assert sdoc.tables, "expected at least one detected table"
    # the split table is stitched: one table spans >1 page and holds all 61 rows
    spanning = [t for t in sdoc.tables if t.page_end > t.page_start]
    assert spanning, "expected a stitched (multi-page) table"
    big = max(sdoc.tables, key=lambda t: len(t.rows))
    assert len(big.rows) >= 61            # header + 60 items, header not duplicated
    assert big.rows[0] == ["Account", "2023", "2022"]
    assert big.rows.count(["Account", "2023", "2022"]) == 1  # repeat header dropped
    assert big.confidence > 0.9           # numbers grounded in the text layer


# ------------------------------------------------- charts are not tables
def test_chart_gridlines_are_not_a_table():
    """A bar chart's rules look like a grid, and the empty result grounds at 1.0.

    Grounding only checks numbers against the text layer, so it reports nothing
    wrong about a table that holds no numbers at all — the junk would be stored
    with a perfect confidence.
    """
    assert _too_empty([["", "", ""], ["", "", ""]])
    assert _too_empty([["", "", "", ""] * 6 + ["38171"]])   # one stray axis label
    assert not _too_empty([["Revenue", "1,234"], ["Cost", "567"]])


def test_sparse_but_real_table_is_kept():
    """Real forms carry padding cells; only near-empty grids are dropped."""
    rows = [["1. Class of shares", "Ordinary shares", "", "", "Type of shares", "Not applicable"],
            ["Stock code", "00700", "", "", "Description", ""]]
    assert not _too_empty(rows)


# ---------------------------------------- the verdict, end to end on real PDFs
def test_a_clean_pdf_passes_the_gate_with_full_coverage():
    pytest.importorskip("reportlab")
    from issuer_data.eval.gold import _table_pdf
    from issuer_data.pdf_extract import extract_structured

    rows = [["Account", "2023"], ["Revenue", "600"], ["Cost", "400"], ["Total", "1,000"]]
    sdoc = extract_structured(_table_pdf(rows))
    assert sdoc.verdict == "pass"
    assert sdoc.validation.coverage.token_recall == 1.0
    # the total row checks out against the rows above it — reference-free evidence
    assert sdoc.validation.arithmetic.checks == 1
    assert sdoc.validation.arithmetic.passed == 1


def test_a_scanned_pdf_fails_loudly_instead_of_returning_empty():
    """The failure this whole gate exists for: an image-only page used to yield a
    clean, empty result that looked like success."""
    pytest.importorskip("reportlab")
    pytest.importorskip("PIL")
    from issuer_data.eval.gold import _scanned_pdf
    from issuer_data.pdf_extract import extract_structured

    content = _scanned_pdf([["Account", "2023"], ["Revenue", "1234"]])
    sdoc = extract_structured(content)
    assert sdoc.tables == []
    assert sdoc.verdict == "fail"
    assert "no text layer" in sdoc.validation.reasons[0]


def test_a_pdf_whose_total_is_wrong_goes_to_review():
    """Nothing is compared to a label here: the document contradicts itself."""
    pytest.importorskip("reportlab")
    from issuer_data.eval.gold import _table_pdf
    from issuer_data.pdf_extract import extract_structured

    rows = [["Account", "2023"], ["Revenue", "600"], ["Cost", "400"], ["Total", "9,999"]]
    sdoc = extract_structured(_table_pdf(rows))
    assert sdoc.verdict == "review"
    assert any("do not add up" in r for r in sdoc.validation.reasons)


def test_validation_can_be_switched_off():
    pytest.importorskip("reportlab")
    from issuer_data.eval.gold import _table_pdf
    from issuer_data.pdf_extract import extract_structured

    sdoc = extract_structured(_table_pdf([["A", "1"], ["B", "2"]]), validate=False)
    assert sdoc.validation is None
    assert sdoc.verdict == "unknown"


def test_the_text_detector_is_an_independent_second_opinion():
    """The consensus check needs a detector that does not share the first one's
    assumptions: this one infers the grid from where the words sit."""
    pytest.importorskip("reportlab")
    from issuer_data.eval.gold import _table_pdf
    from issuer_data.pdf_agreement import agreement
    from issuer_data.pdf_extract import extract_structured

    content = _table_pdf([["Account", "2023"], ["Revenue", "600"], ["Cost", "400"]])
    sdoc = extract_structured(content)
    report = agreement(content, reference=sdoc.tables)
    assert report.other_engine == "text"
    assert report.score is not None
