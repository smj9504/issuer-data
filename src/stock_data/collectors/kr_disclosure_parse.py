"""Parse DART 원문 XML into structured stake-change / treasury-disposal rows.

DART serves each filing's original document as a ZIP of XML whose cells carry
machine-readable codes: `ACODE` names the field on a `<TE>` cell, and `AUNIT` +
`AUNITVALUE` give a coded value with its Korean label on a `<TU>` cell. Because
those codes are stable across issuers (verified against 005930/000660/035420/
051910 filings), these documents are parsed deterministically — unlike the HK
prospectus work, where every document laid its tables out differently and extraction
had to be model-assisted.

What is deliberately *not* done here: deciding whether a holder is a "financial
investor" or "the 대주주". DART already states the relationship (`FLT_CRP_RLT`)
and the entity type (`SPC_TP`); this module stores those codes verbatim and
leaves the judgement to the views in schema.sql, so the definition can change
without re-fetching a single document.
"""

from __future__ import annotations

import io
import re
import zipfile

from ..logging import get_logger

log = get_logger(__name__)

# --- code dictionaries, observed across sampled filings ----------------------
# Not exhaustive by construction: DART can add codes, so parsing keeps any code
# it does not recognise (with its label) rather than dropping the row.
HLD_MTH = {                      # 취득/처분 방법
    "01": "장내매수(+)", "02": "장내매도(-)",
    "11": "장외매수(+)", "12": "장외매도(-)",
    "33": "신규보고(+)", "59": "자사주상여금(+)",
    "69": "주식매수선택권행사(-)", "75": "특별관계해소(-)",
    "86": "주식매수청구권 행사(-)", "90": "주식분할", "96": "회사분할",
    "97": "임원퇴임(-)", "98": "기타(-)", "99": "기타(+)",
    "104": "신규선임(유상취득)(+)",
}
SPC_TP = {"K": "금융기관", "I": "국내법인", "D": "개인(국내)",
          "F": "외국법인", "E": "외국인(개인)"}
FLT_CRP_RLT = {"10": "최대주주", "14": "계열회사등", "16": "기타"}
RPT_DST1 = {"1": "신규", "2": "변동", "3": "변경", "4": "변동ㆍ변경"}

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<TR\b.*?</TR>", re.DOTALL)
_TE_RE = re.compile(r'<TE\b[^>]*ACODE="([A-Z0-9_]+)"[^>]*>(.*?)</TE>', re.DOTALL)
_TU_RE = re.compile(
    r'<TU\b[^>]*AUNIT="([A-Z0-9_]+)"[^>]*AUNITVALUE="([^"]*)"[^>]*>(.*?)</TU>', re.DOTALL
)
# AUNIT and AUNITVALUE also appear in the other order.
_TU_RE_ALT = re.compile(
    r'<TU\b[^>]*AUNITVALUE="([^"]*)"[^>]*AUNIT="([A-Z0-9_]+)"[^>]*>(.*?)</TU>', re.DOTALL
)


def unzip_document(content: bytes) -> str:
    """Decode DART's document.xml ZIP payload to text.

    The endpoint returns a ZIP under a misleading Content-Type, and the XML
    inside is UTF-8 on newer filings and CP949 on older ones.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        raw = z.read(z.namelist()[0])
    for encoding in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", fragment).replace("&cr;", " ")).strip()


def _num(value: str | None) -> float | None:
    """DART numbers carry thousands separators, and '-' means 'not applicable'."""
    if not value:
        return None
    cleaned = value.replace(",", "").replace(" ", "")
    if cleaned in ("-", "", "―", "–"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _iso_date(value: str | None) -> str | None:
    """AUNITVALUE dates are YYYYMMDD; labels are '2026년 07월 29일'."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _row_cells(row: str) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Split one <TR> into ACODE-keyed text cells and AUNIT-keyed coded cells."""
    te = {m.group(1): _text(m.group(2)) for m in _TE_RE.finditer(row)}
    tu: dict[str, tuple[str, str]] = {}
    for m in _TU_RE.finditer(row):
        tu[m.group(1)] = (m.group(2), _text(m.group(3)))
    for m in _TU_RE_ALT.finditer(row):
        tu.setdefault(m.group(2), (m.group(1), _text(m.group(3))))
    return te, tu


def parse_holder_roster(xml: str) -> dict[str, dict]:
    """Map 사업자번호 -> holder attributes from the 특별관계자 table.

    The relationship (FLT_CRP_RLT) and entity type (SPC_TP) live on a *roster*
    table listing each 특별관계자 once, not on the 변동 rows — so the codes that
    decide "최대주주 vs 계열회사 vs 금융기관" have to be joined onto the change
    rows by SPC_ID2 rather than read off them.

    Keyed by SPC_ID2 (사업자등록번호), with a name fallback for filers that omit
    it, since the id is the only stable identifier across name spellings.
    """
    roster: dict[str, dict] = {}
    for row in _ROW_RE.findall(xml):
        te, tu = _row_cells(row)
        name = te.get("SPC_NM")
        if not name or "HLD_MTH" in tu:
            continue  # a 변동 row, not a roster row
        if "SPC_TP" not in tu and "FLT_CRP_RLT" not in tu:
            continue
        type_code, type_label = tu.get("SPC_TP") or ("", None)
        rel_code, rel_label = tu.get("FLT_CRP_RLT") or ("", None)
        entry = {
            "holder_type": type_code or None,
            "holder_type_label": type_label or SPC_TP.get(type_code),
            "relation": rel_code or None,
            "relation_label": rel_label or FLT_CRP_RLT.get(rel_code),
        }
        holder_id = te.get("SPC_ID2") or ""
        if holder_id:
            roster[holder_id] = entry
        roster.setdefault(f"name:{name}", entry)
    return roster


def parse_stake_changes(xml: str) -> list[dict]:
    """Extract 변동 rows from a 주식등의대량보유상황보고서 (D001).

    A 변동 row is identified structurally — it holds a holder name (SPC_NM) and a
    method (HLD_MTH) — rather than by table position, since the document contains
    several tables whose order varies by filer. Holder attributes are joined from
    the roster table (see parse_holder_roster).
    """
    roster = parse_holder_roster(xml)
    rows: list[dict] = []
    for row in _ROW_RE.findall(xml):
        te, tu = _row_cells(row)
        name = te.get("SPC_NM")
        if not name or "HLD_MTH" not in tu:
            continue
        method_code, method_label = tu["HLD_MTH"]
        holder_id = te.get("SPC_ID2") or ""
        attrs = roster.get(holder_id) or roster.get(f"name:{name}") or {}
        rows.append({
            "holder_name": name,
            "holder_id": holder_id,
            "holder_type": attrs.get("holder_type"),
            "holder_type_label": attrs.get("holder_type_label"),
            "relation": attrs.get("relation"),
            "relation_label": attrs.get("relation_label"),
            "method": method_code or None,
            "method_label": method_label or HLD_MTH.get(method_code),
            # '' not None: stock_kind is part of the storage primary key, where a
            # NULL would stop two rows from ever colliding and defeat dedup.
            "stock_kind": (tu.get("STK_KND") or ("", ""))[0] or "",
            "change_date": _iso_date((tu.get("MDF_DT") or ("", ""))[0])
                           or _iso_date(te.get("MDF_DT")),
            "shares_before": _num(te.get("BFR_MDF_CNT")),
            "shares_delta": _num(te.get("MDF_SDK_CNT")),
        })
    if not rows:
        log.debug("no 변동 rows found; document may be a 신규 report with no changes")
    return rows


def parse_report_meta(xml: str) -> dict:
    """Header fields shared by the whole document (보고구분 etc.)."""
    _, tu = _row_cells(xml)
    report_code = (tu.get("RPT_DST1") or ("", ""))[0]
    return {
        "report_type": (tu.get("RPT_DST1") or ("", None))[1]
                       or RPT_DST1.get(report_code),
    }


def parse_treasury_disposal(xml: str) -> dict:
    """Extract 자기주식 처분/취득 결정 fields.

    Unlike a stake change this carries an execution price (SEL_OSTK_SPRC) and the
    counterparty (DSPS_PARN), which is what price-consistency checking needs.
    """
    te = {m.group(1): _text(m.group(2)) for m in _TE_RE.finditer(xml)}
    return {
        "shares": _num(te.get("SEL_OSTK")),
        "unit_price": _num(te.get("SEL_OSTK_SPRC")),
        "total_amount": _num(te.get("SEL_OSTK_PRC")),
        "counterparty": te.get("DSPS_PARN") or None,
        "purpose": te.get("SEL_PPS") or None,
        "method": te.get("SEL_MTH") or None,
    }
