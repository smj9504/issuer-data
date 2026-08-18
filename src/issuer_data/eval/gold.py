"""Gold cases for the PDF eval harness: synthetic (exact ground truth) + on-disk.

Synthetic cases are authored from known table/paragraph data with reportlab, so
the label *is* the source — no hand annotation, exact ground truth. They span a
{country/language × layout} matrix (US/en, KR/ko, HK/zh × single-table,
split-table, multi-column prose). A case whose CJK font or reportlab is
unavailable self-skips (returns None), keeping CI green.

Real labelled documents can be dropped under ``data/eval/<name>/`` as
``doc.pdf`` + ``expected.json`` ({"category", "tables": [[...]], "paragraphs": []})
and are picked up by ``load_gold_dir`` with no code change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GoldCase:
    name: str
    category: str
    pdf_bytes: bytes
    tables: list[list[list[str]]] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)


# --------------------------------------------------------------- pdf builders
def _reportlab():
    try:
        import reportlab  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _register_cid(font_name: str) -> bool:
    """Register an Adobe CID font (no external file); return False if unavailable."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        return True
    except Exception:  # noqa: BLE001
        return False


def _table_pdf(rows, *, font=None, repeat=1) -> bytes:
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import LongTable, SimpleDocTemplate, TableStyle

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=36, bottomMargin=36)
    data = [list(r) for r in rows]
    if repeat > 1:
        header, body = data[0], data[1:]
        data = [header] + body * repeat
    tbl = LongTable(data, repeatRows=1)
    style = [("GRID", (0, 0), (-1, -1), 0.5, colors.black)]
    if font:
        style.append(("FONTNAME", (0, 0), (-1, -1), font))
    tbl.setStyle(TableStyle(style))
    doc.build([tbl])
    return buf.getvalue()


def _prose_pdf(paragraphs, *, font=None) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=36, bottomMargin=36)
    style = getSampleStyleSheet()["BodyText"]
    if font:
        style = style.clone("cjk", fontName=font)
    flow = []
    for para in paragraphs:
        flow.append(Paragraph(para, style))
        flow.append(Spacer(1, 12))
    doc.build(flow)
    return buf.getvalue()


# --------------------------------------------------------------- case builders
_US_TABLE = [["Account", "2023", "2022"], ["Revenue", "1234", "1180"],
             ["Operating income", "456", "410"], ["Net income", "321", "298"]]
_KR_TABLE = [["계정", "2023", "2022"], ["매출액", "1234", "1180"],
             ["영업이익", "456", "410"], ["당기순이익", "321", "298"]]
_HK_TABLE = [["項目", "2023", "2022"], ["收入", "1234", "1180"],
             ["經營溢利", "456", "410"], ["純利", "321", "298"]]
_EN_PROSE = [
    ("The company delivered record revenue this year, driven by strong demand across "
     "all of its principal operating segments and continued disciplined cost control "
     "that widened operating margins relative to the prior comparable period."),
    ("Management remains cautiously optimistic about the coming fiscal year despite a "
     "more uncertain macroeconomic backdrop, and expects capital expenditure to normalise "
     "toward historical averages as the current investment cycle matures."),
]


def synthetic_cases() -> list[GoldCase]:
    """Build the synthetic gold matrix; skip cases whose fonts are unavailable."""
    if not _reportlab():
        return []
    cases: list[GoldCase] = []
    # US / English — single, split (forced multi-page), and prose continuity.
    cases.append(GoldCase("us_single", "US/en/single-table",
                          _table_pdf(_US_TABLE), tables=[_US_TABLE]))
    big = [_US_TABLE[0]] + _US_TABLE[1:] * 20
    cases.append(GoldCase("us_split", "US/en/split-table",
                          _table_pdf(_US_TABLE, repeat=20), tables=[big]))
    cases.append(GoldCase("us_prose", "US/en/multi-column-prose",
                          _prose_pdf(_EN_PROSE), paragraphs=_EN_PROSE))
    # Korean / Chinese single tables (CID fonts).
    if _register_cid("HYSMyeongJo-Medium"):
        cases.append(GoldCase("kr_single", "KR/ko/single-table",
                              _table_pdf(_KR_TABLE, font="HYSMyeongJo-Medium"),
                              tables=[_KR_TABLE]))
    if _register_cid("STSong-Light"):
        cases.append(GoldCase("hk_single", "HK/zh/single-table",
                              _table_pdf(_HK_TABLE, font="STSong-Light"),
                              tables=[_HK_TABLE]))
    return cases


def load_gold_dir(path) -> list[GoldCase]:
    """Load real labelled cases from data/eval/<name>/{doc.pdf, expected.json}."""
    root = Path(path)
    if not root.is_dir():
        return []
    cases: list[GoldCase] = []
    for sub in sorted(root.iterdir()):
        pdf, meta = sub / "doc.pdf", sub / "expected.json"
        if not (pdf.is_file() and meta.is_file()):
            continue
        spec = json.loads(meta.read_text(encoding="utf-8"))
        cases.append(GoldCase(
            name=sub.name, category=spec.get("category", sub.name),
            pdf_bytes=pdf.read_bytes(), tables=spec.get("tables", []),
            paragraphs=spec.get("paragraphs", []),
        ))
    return cases
