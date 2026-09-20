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


def _scanned_pdf(rows) -> bytes | None:
    """The same table as a page image — no text layer at all.

    This is the failure the local engine cannot parse and, before validation,
    reported as a clean empty result. Keeping it in the gold set means the eval
    always contains at least one document that *must* be caught, so a gate that
    silently stops working shows up as `missed` instead of a perfect score.
    """
    from io import BytesIO

    try:
        from PIL import Image, ImageDraw
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except Exception:  # noqa: BLE001
        return None

    img = Image.new("RGB", (1200, 400), "white")
    draw = ImageDraw.Draw(img)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            draw.text((40 + c * 320, 40 + r * 60), str(cell), fill="black")
        draw.line((30, 30 + r * 60, 1170, 30 + r * 60), fill="black")
    buf = BytesIO()
    page = canvas.Canvas(buf, pagesize=letter)
    page.drawImage(ImageReader(img), 40, 400, width=520, height=180)
    page.showPage()
    page.save()
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


# Korean is space-delimited between 어절 while Chinese is not, so a line break
# inside a paragraph has to be rejoined differently for each. Without a case per
# language the prose score reports 1.000 on English alone and says nothing about
# the two markets this project actually collects.
_KO_PROSE = [
    ("당사는 2026년 1분기 연결기준 매출액 300조원을 달성하였으며 이는 전년 동기 대비 "
     "9퍼센트 증가한 수치로서 주력 사업부문 전반의 견조한 수요와 지속적인 비용 통제에 "
     "기인한 것입니다."),
    ("경영진은 거시경제 불확실성이 확대되는 가운데에서도 차기 회계연도 전망을 신중하게 "
     "낙관하고 있으며 현재의 투자 주기가 성숙함에 따라 자본적 지출은 과거 평균 수준으로 "
     "정상화될 것으로 예상하고 있습니다."),
]
_ZH_PROSE = [
    ("本集團於二零二六年第一季度錄得收入人民幣三千億元較去年同期增長百分之九主要由於各主要"
     "經營分部需求強勁以及持續嚴格控制成本使經營溢利率較去年同期有所擴大。"),
    ("儘管宏觀經濟環境的不確定性有所增加管理層對下一財政年度仍持審慎樂觀態度並預期隨著本輪"
     "投資週期趨於成熟資本開支將回復至過往平均水平。"),
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
    cases.append(GoldCase("us_prose", "US/en/prose",
                          _prose_pdf(_EN_PROSE), paragraphs=_EN_PROSE))
    # Korean / Chinese single tables (CID fonts).
    if _register_cid("HYSMyeongJo-Medium"):
        cases.append(GoldCase("kr_single", "KR/ko/single-table",
                              _table_pdf(_KR_TABLE, font="HYSMyeongJo-Medium"),
                              tables=[_KR_TABLE]))
        cases.append(GoldCase("kr_prose", "KR/ko/prose",
                              _prose_pdf(_KO_PROSE, font="HYSMyeongJo-Medium"),
                              paragraphs=_KO_PROSE))
    scanned = _scanned_pdf(_US_TABLE)
    if scanned:
        cases.append(GoldCase("us_scanned", "US/en/scanned",
                              scanned, tables=[_US_TABLE]))
    if _register_cid("STSong-Light"):
        cases.append(GoldCase("hk_single", "HK/zh/single-table",
                              _table_pdf(_HK_TABLE, font="STSong-Light"),
                              tables=[_HK_TABLE]))
        cases.append(GoldCase("hk_prose", "HK/zh/prose",
                              _prose_pdf(_ZH_PROSE, font="STSong-Light"),
                              paragraphs=_ZH_PROSE))
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
