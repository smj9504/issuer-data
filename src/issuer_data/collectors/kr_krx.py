"""KRX collector via pykrx: Korean master (ticker list + names) and OHLCV prices.

pykrx scrapes KRX/Naver endpoints (no key). It returns Korean-column DataFrames
with a DatetimeIndex and no adjusted close.
"""

from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from ..models import Price, SecurityRecord
from ..utils.dates import compact, default_range, to_iso
from .base import BaseCollector

log = get_logger(__name__)

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
        df = self.stock.get_market_ohlcv(compact(start), compact(end), symbol)
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
