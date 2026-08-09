"""DART (Korea) collector via OpenDartReader: master (corp_code), financials, filings.

Requires a free OpenDART API key (ISSUER_DART_API_KEY). Without it, the collector
raises NotSupportedError and the orchestrator skips KR financials/filings cleanly.
"""

from __future__ import annotations

import datetime as _dt

from ..config import Settings
from ..logging import get_logger
from ..models import FinancialFact, Filing, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

# DART periodic-report codes -> our fiscal_period
_REPRT = {"11011": "FY", "11014": "Q3", "11012": "H1", "11013": "Q1"}
# statement code (sj_div) -> our statement_type
_SJ = {"IS": "IS", "CIS": "IS", "BS": "BS", "CF": "CF"}
_DOC_URL = "https://opendart.fss.or.kr/api/document.xml?crtfc_key={key}&rcept_no={rcept}"
_VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}"


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
                try:
                    df = self.dart.finstate_all(symbol, year, reprt_code=reprt_code)
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
                            statement_type=_SJ.get(sj),
                            account=str(account),
                            account_local=str(account),
                            value=val,
                            currency="KRW",
                            unit="KRW",
                            period_end=None,
                            source="dart",
                        )
                    )
        return out

    # --------------------------------------------------------------- filings
    def fetch_filings(self, symbol: str, start: str, end: str) -> list[Filing]:
        start, end = default_range(start, end, default_years=2)
        try:
            df = self.dart.list(symbol, start=start, end=end, kind="A", final=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("DART list failed for %s: %s", symbol, exc)
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
