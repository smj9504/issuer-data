"""KRX collector via pykrx: Korean master (ticker list + names) and OHLCV prices.

pykrx scrapes KRX/Naver endpoints (no key). It returns Korean-column DataFrames
with a DatetimeIndex and no adjusted close.
"""

from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from ..models import Price, SecurityRecord
from ..utils.dates import compact, default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

# Major KR indices for current-membership checks (index code -> our label).
_KR_INDICES = {"1028": "KOSPI200", "1035": "KRX100", "2203": "KOSDAQ150"}

# pykrx OHLCV Korean column names -> our fields
_COLMAP = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
}


class KrxCollector(BaseCollector):
    market = "KR"
    source = "krx"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        from pykrx import stock  # imported here so the dep is only needed for KR

        self.stock = stock

    # --------------------------------------------------------------- master
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        wanted = {s for s in symbols} if symbols else None
        out: list[SecurityRecord] = []
        for market_name, exch in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")):
            try:
                tickers = self.stock.get_market_ticker_list(market=market_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("KRX ticker list failed for %s: %s", market_name, exc)
                continue
            for ticker in tickers:
                if wanted is not None and ticker not in wanted:
                    continue
                try:
                    name = self.stock.get_market_ticker_name(ticker)
                except Exception:  # noqa: BLE001
                    name = None
                out.append(
                    SecurityRecord(
                        market="KR",
                        symbol=ticker,
                        local_name=name,
                        name=name,
                        country="KR",
                        exchange=exch,
                        security_type="COMMON",
                        currency="KRW",
                        is_primary=True,
                        source="krx",
                    )
                )
                if wanted is not None and len(out) >= len(wanted):
                    break
        return out

    # --------------------------------------------------------------- prices
    def fetch_prices(self, symbol: str, start: str, end: str) -> list[Price]:
        start, end = default_range(start, end)
        # adjusted=False (raw OHLC). pykrx's default adjusted=True path hits KRX's
        # 수정주가 endpoint (adjStkPrc=2), which returns an empty frame in pykrx
        # 1.2.8 — the collector silently got 0 rows. We store unadjusted OHLC and
        # leave adj_close=None, so the raw series is both correct and reliable.
        df = self.stock.get_market_ohlcv(compact(start), compact(end), symbol, adjusted=False)
        if df is None or df.empty:
            return []
        df = df.rename(columns=_COLMAP)
        out: list[Price] = []
        for idx, row in df.iterrows():
            trade_date = to_iso(idx)
            close = _num(row.get("close"))
            if close is None:
                continue
            out.append(
                Price(
                    symbol=symbol,
                    market="KR",
                    trade_date=trade_date,
                    open=_num(row.get("open")),
                    high=_num(row.get("high")),
                    low=_num(row.get("low")),
                    close=close,
                    volume=_int(row.get("volume")),
                    adj_close=None,
                    currency="KRW",
                    source="krx",
                )
            )
        return out

    # --------------------------------------------------- Extension B coverage
    def fetch_daily_metrics(self, symbol: str, start: str, end: str):
        """Foreign-ownership % + listed shares from KRX's foreign-investment table.

        KRX's data portal requires a (free) member login (KRX_ID/KRX_PW); without
        it pykrx yields nothing → we raise NotSupportedError so the run is skipped
        cleanly rather than silently empty.
        """
        from ..models import DailyMetric

        _, end = default_range(start, end)
        date = compact(end)
        last_exc: Exception | None = None
        for market_name in ("KOSPI", "KOSDAQ"):
            try:
                df = self.stock.get_exhaustion_rates_of_foreign_investment_by_ticker(
                    date, market_name)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            if df is None or getattr(df, "empty", True) or symbol not in df.index:
                continue
            row = df.loc[symbol]
            return [DailyMetric(
                symbol=symbol, market="KR", metric_date=to_iso(end),
                shares_outstanding=_num(_col(row, "상장주식수")),
                foreign_own_pct=_num(_col(row, "지분율")),
                currency="KRW", source="krx")]
        if last_exc is not None:
            raise NotSupportedError(
                f"KRX foreign-investment needs KRX_ID/KRX_PW: {last_exc}")
        return []

    def fetch_index_membership(self, symbol: str):
        """Current membership of the major KR indices (snapshot, not history)."""
        from ..models import IndexMembership

        out: list = []
        last_exc: Exception | None = None
        for code, name in _KR_INDICES.items():
            try:
                members = self.stock.get_index_portfolio_deposit_file(code)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            if members and symbol in set(members):
                out.append(IndexMembership(symbol=symbol, market="KR",
                                           index_name=name, source="krx"))
        if not out and last_exc is not None:
            raise NotSupportedError(
                f"KRX index constituents need KRX_ID/KRX_PW: {last_exc}")
        return out


def _col(row, name):
    """Read a column from a pandas Series row, tolerating label drift."""
    labels = list(getattr(row, "index", []))
    if name in labels:
        return row[name]
    for label in labels:
        if name in str(label):
            return row[label]
    return None


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None
