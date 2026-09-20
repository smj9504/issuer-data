"""KRX collector via pykrx: Korean master (ticker list + names) and OHLCV prices.

pykrx scrapes KRX/Naver endpoints (no key). It returns Korean-column DataFrames
with a DatetimeIndex and no adjusted close.
"""

from __future__ import annotations

import sys

from ..config import Settings
from ..logging import get_logger
from ..models import Price, SecurityRecord
from ..utils.dates import compact, default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)


def _import_pykrx_stock():
    """Import pykrx.stock, surviving the login it runs at import time.

    pykrx builds its session in module scope (`webio.py`: `_session =
    build_krx_session()`), reading KRX_ID/KRX_PW via `os.getenv` in that
    function's *default arguments* — so merely importing pykrx attempts a login.
    Every branch of it (missing creds, bad creds, and success alike) then
    `print`s Korean text, which raises UnicodeEncodeError on a Windows console
    still in a legacy code page, killing the import — and with it any script
    that imports this module, even one that never touches KRX.

    So: force the streams to UTF-8 first, and treat a failed import as "KRX
    unavailable" rather than letting it take the process down.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                log.debug("could not switch %s to UTF-8: %s", stream, exc)
    try:
        from pykrx import stock  # imported here so the dep is only needed for KR
    except Exception as exc:
        raise NotSupportedError(
            f"pykrx is unavailable ({type(exc).__name__}: {exc}). KRX login runs at "
            "import time; check KRX_ID/KRX_PW or use --source yfinance for KR."
        ) from exc
    return stock


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
        self.stock = _import_pykrx_stock()

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
        s, e = compact(start), compact(end)
        # adjusted=True is the reliable path — verified live (2026-09-03) across
        # KOSPI/KOSDAQ tickers and across a known 50:1 split (Samsung Electronics,
        # 2018-05-04): pre-split closes came back already rescaled continuous with
        # post-split ones, confirming this path genuinely divides out corporate
        # actions rather than just relabeling raw prices.
        #
        # adjusted=False was previously assumed to be the reliable side (an older
        # comment here claimed adjusted=True returned an empty frame in pykrx
        # 1.2.8) but live-tested the opposite: adjusted=False now returns an empty
        # (0, 0) frame — pykrx or KRX must have changed sides since that comment
        # was written. Still attempted first (cheap, and would be the source of
        # truth for raw OHLC if it starts working again) with adjusted=True as the
        # fallback for the whole row when it comes back empty — meaning close and
        # adj_close are then numerically identical for that row, which is honest:
        # the unadjusted figure genuinely isn't available, not fabricated to look
        # different from the adjusted one.
        raw_df = self.stock.get_market_ohlcv(s, e, symbol, adjusted=False)
        adj_df = self.stock.get_market_ohlcv(s, e, symbol, adjusted=True)
        if adj_df is None or adj_df.empty:
            return []
        adj_df = adj_df.rename(columns=_COLMAP)
        raw_df = raw_df.rename(columns=_COLMAP) if raw_df is not None and not raw_df.empty else None

        out: list[Price] = []
        for idx, adj_row in adj_df.iterrows():
            trade_date = to_iso(idx)
            adj_close = _num(adj_row.get("close"))
            if adj_close is None:
                continue
            raw_row = raw_df.loc[idx] if raw_df is not None and idx in raw_df.index else adj_row
            close = _num(raw_row.get("close"))
            if close is None:
                close = adj_close
            out.append(
                Price(
                    symbol=symbol,
                    market="KR",
                    trade_date=trade_date,
                    open=_num(raw_row.get("open")),
                    high=_num(raw_row.get("high")),
                    low=_num(raw_row.get("low")),
                    close=close,
                    volume=_int(raw_row.get("volume")),
                    adj_close=adj_close,
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
