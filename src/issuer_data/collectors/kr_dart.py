"""DART (Korea) collector via OpenDartReader: master (corp_code), financials, filings.

Requires a free OpenDART API key (ISSUER_DART_API_KEY). Without it, the collector
raises NotSupportedError and the orchestrator skips KR financials/filings cleanly.
"""

from __future__ import annotations

import datetime as _dt

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Filing, FinancialFact, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

# DART periodic-report codes -> our fiscal_period
_REPRT = {"11011": "FY", "11014": "Q3", "11012": "H1", "11013": "Q1"}
# statement code (sj_div) -> our statement_type
_SJ = {"IS": "IS", "CIS": "IS", "BS": "BS", "CF": "CF"}
_DOC_API = "https://opendart.fss.or.kr/api/document.xml"
_DOC_URL = "https://opendart.fss.or.kr/api/document.xml?crtfc_key={key}&rcept_no={rcept}"
_VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}"

# DART 공시유형. A has always been this collector's default; D (지분공시) carries
# 주식등의대량보유상황보고서 and B (주요사항보고) carries 자기주식처분결정, neither of
# which appears in an A listing at all.
_FILING_KINDS = {
    "A": "정기공시",
    "B": "주요사항보고",
    "C": "발행공시",
    "D": "지분공시",
    "E": "기타공시",
    "F": "외부감사관련",
    "G": "펀드공시",
    "H": "자산유동화",
    "I": "거래소공시",
    "J": "공정위공시",
}
_DEFAULT_FILING_KIND = "A"


class DartCollector(BaseCollector):
    market = "KR"
    source = "dart"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.dart_api_key
        if not self.api_key:
            raise NotSupportedError(
                "DART requires ISSUER_DART_API_KEY (free at opendart.fss.or.kr)"
            )
        import OpenDartReader

        self.dart = OpenDartReader(self.api_key)
        # OpenDartReader has no 원문 accessor, so raw document.xml calls go through
        # the project's rate-limited client rather than bare requests.
        self.client = HttpClient(rate_limit=settings.default_rate_limit)

    # --------------------------------------------------------------- master
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        df = self.dart.corp_codes
        if df is None or df.empty:
            return []
        # listed companies only (non-empty stock_code)
        listed = df[df["stock_code"].astype(str).str.strip().str.len() == 6]
        wanted = {s.zfill(6) for s in symbols} if symbols else None
        out: list[SecurityRecord] = []
        for _, row in listed.iterrows():
            stock_code = str(row["stock_code"]).strip()
            if wanted is not None and stock_code not in wanted:
                continue
            out.append(
                SecurityRecord(
                    market="KR",
                    symbol=stock_code,
                    local_name=str(row["corp_name"]),
                    name=str(row["corp_name"]),
                    country="KR",
                    corp_code=str(row["corp_code"]),
                    currency="KRW",
                    security_type="COMMON",
                    is_primary=True,
                    source="dart",
                )
            )
        return out

    # ------------------------------------------------------------ financials
    def fetch_financials(self, symbol: str, years: int | None = None) -> list[FinancialFact]:
        years = years or 3
        this_year = _dt.date.today().year
        out: list[FinancialFact] = []
        for year in range(this_year - years, this_year + 1):
            for reprt_code, period in _REPRT.items():
                # CFS = consolidated (연결), OFS = separate (별도) — store both.
                for fs_div in ("CFS", "OFS"):
                    try:
                        df = self.dart.finstate_all(symbol, year, reprt_code=reprt_code,
                                                    fs_div=fs_div)
                    except Exception:  # noqa: BLE001
                        df = None
                    if df is None or getattr(df, "empty", True):
                        continue
                    for _, row in df.iterrows():
                        val = _parse_amount(row.get("thstrm_amount"))
                        account = row.get("account_nm")
                        if account is None:
                            continue
                        sj = str(row.get("sj_div", "")).upper()
                        out.append(
                            FinancialFact(
                                symbol=symbol,
                                market="KR",
                                fiscal_year=year,
                                fiscal_period=period,
                                fs_scope=fs_div,
                                statement_type=_SJ.get(sj),
                                account=str(account),
                                account_local=str(account),
                                value=val,
                                currency="KRW",
                                unit="KRW",
                                period_end=_dart_period_end(row.get("thstrm_dt")),
                                source="dart",
                            )
                        )
        return out

    # --------------------------------------------------------------- filings
    def fetch_filings(
        self, symbol: str, start: str, end: str, kind: str | None = None
    ) -> list[Filing]:
        """DART filings for `symbol`.

        `kind` is DART's 공시유형: A=정기공시, B=주요사항보고, C=발행공시,
        D=지분공시, E=기타, F=외부감사, G=펀드, H=자산유동화, I=거래소,
        J=공정위. It defaults to A, which is what this collector has always
        returned — but 지분공시(D, e.g. 주식등의대량보유상황보고서) and
        주요사항보고(B, e.g. 자기주식처분결정) are simply absent from an A
        listing, so anything working on stake changes has to ask for them.
        """
        start, end = default_range(start, end, default_years=2)
        kind = (kind or _DEFAULT_FILING_KIND).upper()
        if kind not in _FILING_KINDS:
            raise ValueError(
                f"Unknown DART filing kind {kind!r}; expected one of "
                + ", ".join(f"{k} ({v})" for k, v in _FILING_KINDS.items())
            )
        try:
            df = self.dart.list(symbol, start=start, end=end, kind=kind, final=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("DART list failed for %s (kind=%s): %s", symbol, kind, exc)
            return []
        if df is None or getattr(df, "empty", True):
            return []
        out: list[Filing] = []
        for _, row in df.iterrows():
            rcept = str(row.get("rcept_no"))
            out.append(
                Filing(
                    symbol=symbol,
                    market="KR",
                    filing_id=rcept,
                    filed_date=to_iso(row.get("rcept_dt")),
                    filing_type=row.get("report_nm"),
                    title=row.get("report_nm"),
                    url=_VIEW_URL.format(rcept=rcept),
                    source="dart",
                    doc_urls=[_DOC_URL.format(key=self.api_key, rcept=rcept)],
                )
            )
        return out


    # ------------------------------------------------- 원문 기반 지분 변동/자사주
    def _document_xml(self, rcept_no: str) -> str | None:
        """Fetch and decode one filing's 원문 (document.xml returns a ZIP)."""
        from ..kr_disclosure_parse import unzip_document

        try:
            resp = self.client.get(
                _DOC_API, params={"crtfc_key": self.api_key, "rcept_no": rcept_no}
            )
            return unzip_document(resp.content)
        except Exception as exc:  # noqa: BLE001
            log.warning("DART 원문 fetch failed for %s: %s", rcept_no, exc)
            return None

    def fetch_stake_changes(self, symbol: str, start: str, end: str):
        """변동 rows from every 주식등의대량보유상황보고서 (D001) in the range.

        Two calls per filing: the D listing, then each document's 원문. Only the
        대량보유 reports are opened — 임원·주요주주 reports dominate a D listing by
        count and carry no 변동명세, so opening them would multiply the request
        count for nothing.
        """
        from ..kr_disclosure_parse import parse_report_meta, parse_stake_changes
        from ..models import StakeChange

        out: list[StakeChange] = []
        for filing in self.fetch_filings(symbol, start, end, kind="D"):
            if "대량보유" not in (filing.filing_type or ""):
                continue
            xml = self._document_xml(filing.filing_id)
            if not xml:
                continue
            report_type = parse_report_meta(xml).get("report_type")
            for row in parse_stake_changes(xml):
                if not row.get("change_date"):
                    continue  # a 변동 line with no date cannot be placed in time
                out.append(StakeChange(
                    symbol=symbol, market="KR", source="dart",
                    rcept_no=filing.filing_id, report_type=report_type, **row,
                ))
        return out

    def fetch_treasury_disposals(self, symbol: str, start: str, end: str):
        """자기주식 취득/처분 결정 and 결과보고서 (주요사항보고 B)."""
        from ..kr_disclosure_parse import parse_treasury_disposal
        from ..models import TreasuryDisposal

        out: list[TreasuryDisposal] = []
        for filing in self.fetch_filings(symbol, start, end, kind="B"):
            name = filing.filing_type or ""
            if "자기주식" not in name:
                continue
            xml = self._document_xml(filing.filing_id)
            if not xml:
                continue
            fields = parse_treasury_disposal(xml)
            if not any(v is not None for v in fields.values()):
                continue
            out.append(TreasuryDisposal(
                symbol=symbol, market="KR", source="dart",
                rcept_no=filing.filing_id,
                report_date=filing.filed_date or "",
                report_kind=_treasury_kind(name),
                **fields,
            ))
        return out

    # --------------------------------------------------- Extension B coverage
    def fetch_ownership(self, symbol: str):
        """대량보유 상황보고 (5%+ major-shareholder disclosures, majorstock.json)."""
        from ..models import OwnershipRow

        df = self._report_df("major_shareholders", symbol)
        out: list = []
        for row in df:
            date = to_iso(_dot_date(row.get("rcept_dt")))
            holder = row.get("repror") or row.get("report_tp")
            if not holder:
                continue
            out.append(OwnershipRow(
                symbol=symbol, market="KR", as_of_date=date or "",
                holder_name=str(holder), holder_type=row.get("report_tp"),
                shares=_parse_amount(row.get("stkqy")),
                pct=_parse_amount(row.get("stkrt")), source="dart"))
        return out

    def fetch_insiders(self, symbol: str, start: str, end: str):
        """임원·주요주주 소유보고 (insider/exec ownership reports, elestock.json)."""
        from ..models import InsiderTrade

        start, end = default_range(start, end, default_years=2)
        df = self._report_df("major_shareholders_exec", symbol)
        out: list = []
        seq_by_filing: dict[str, int] = {}
        for row in df:
            date = to_iso(_dot_date(row.get("rcept_dt")))
            if date and not (start <= date <= end):
                continue
            insider = row.get("repror")
            if not insider:
                continue
            rcept = str(row.get("rcept_no") or "")
            change = _parse_amount(row.get("sp_stock_lmp_irds_cnt"))
            held = _parse_amount(row.get("sp_stock_lmp_cnt"))
            if change is None or change == 0:
                txn_type, shares = "hold", held
            else:
                txn_type = "buy" if change > 0 else "sell"
                shares = abs(change)
            seq = seq_by_filing.get(rcept, 0)
            seq_by_filing[rcept] = seq + 1
            out.append(InsiderTrade(
                symbol=symbol, market="KR", filed_date=date or "",
                insider=str(insider), relation=row.get("isu_exctv_ofcps") or "임원",
                txn_type=txn_type, txn_seq=seq, shares=shares, price=None,
                filing_id=rcept, source="dart"))
        return out

    def _report_df(self, method_name: str, symbol: str) -> list[dict]:
        """Call an OpenDartReader 지분공시 method and return a list of row dicts."""
        method = getattr(self.dart, method_name, None)
        if method is None:
            raise NotSupportedError(f"OpenDartReader has no {method_name}()")
        try:
            df = method(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("DART %s failed for %s: %s", method_name, symbol, exc)
            return []
        if df is None or getattr(df, "empty", True):
            return []
        # pandas fills absent cells with NaN; scrub to None (NaN != NaN).
        return [{k: (None if isinstance(v, float) and v != v else v)
                 for k, v in rec.items()} for rec in df.to_dict("records")]


def _dot_date(v) -> str | None:
    """Normalize a DART dotted date ('2023.12.31' / '2023-12-31') to ISO-ready form."""
    if v is None:
        return None
    s = str(v).strip()
    return s.replace(".", "-").replace("/", "-") if s else None


def _dart_period_end(v) -> str | None:
    """Parse DART thstrm_dt into an ISO period-end date.

    Handles a single date ('2023.12.31') and a range ('2023.01.01 ~ 2023.12.31'),
    taking the end date; normalizes dots to dashes.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if "~" in s:
        s = s.split("~")[-1].strip()
    s = s.replace(".", "-").replace("/", "-")
    return to_iso(s)


def _parse_amount(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _treasury_kind(report_name: str) -> str:
    """Bucket a 주요사항보고 title into 처분결정 / 취득결정 / 결과보고서.

    The decision and the result are separate filings; keeping them apart is what
    lets a decided price be compared against what was actually executed.
    """
    if "결과보고서" in report_name:
        return "결과보고서"
    if "처분결정" in report_name:
        return "처분결정"
    if "취득결정" in report_name:
        return "취득결정"
    return report_name.strip() or "기타"
