"""Offline tests for the Phase-18 coverage implementations (FMP + DART)."""

import pandas as pd

from issuer_data.collectors.base import NotSupportedError
from issuer_data.collectors.fmp import FmpCollector
from issuer_data.collectors.kr_dart import DartCollector
from issuer_data.collectors.kr_krx import KrxCollector
from issuer_data.config import Settings


# --------------------------------------------------------------------- FMP
def _fmp(monkeypatch, responses):
    c = FmpCollector(Settings(fmp_api_key="test"))
    c._current_market = "US"
    monkeypatch.setattr(c, "_get", lambda path, **kw: responses.get(path, []))
    return c


def test_fmp_earnings(monkeypatch):
    c = _fmp(monkeypatch, {
        "earnings": [
            {"date": "2024-11-01", "epsActual": 1.64, "epsEstimated": 1.60},
            {"date": "2025-02-01", "epsActual": None, "epsEstimated": 2.35},  # upcoming
            {"date": None, "epsActual": 1.0},  # dropped: no date
        ],
    })
    events = c.fetch_earnings("AAPL")
    assert len(events) == 2
    e0 = next(e for e in events if e.event_date == "2024-11-01")
    assert e0.eps_actual == 1.64 and e0.eps_estimate == 1.60
    assert e0.event_type == "earnings"


def test_fmp_index_membership(monkeypatch):
    c = _fmp(monkeypatch, {
        "sp500-constituent": [
            {"symbol": "AAPL", "dateFirstAdded": "1982-11-30"},
            {"symbol": "MSFT", "dateFirstAdded": "1994-06-01"},
        ],
        "nasdaq-constituent": [{"symbol": "AAPL", "dateFirstAdded": None}],
        "dowjones-constituent": [{"symbol": "MSFT"}],  # AAPL not in Dow
    })
    rows = c.fetch_index_membership("AAPL")
    idx = {r.index_name for r in rows}
    assert idx == {"S&P500", "NASDAQ100"}  # not DOWJONES
    sp = next(r for r in rows if r.index_name == "S&P500")
    assert sp.added == "1982-11-30"


def test_fmp_index_membership_us_only(monkeypatch):
    c = _fmp(monkeypatch, {})
    c._current_market = "KR"
    import pytest

    from issuer_data.collectors.base import NotSupportedError
    with pytest.raises(NotSupportedError):
        c.fetch_index_membership("005930")


# -------------------------------------------------------------------- DART
class _FakeDart:
    def __init__(self, major=None, exec_=None):
        self._major, self._exec = major, exec_

    def major_shareholders(self, corp):
        return pd.DataFrame(self._major or [])

    def major_shareholders_exec(self, corp):
        return pd.DataFrame(self._exec or [])


def _dart(fake):
    c = object.__new__(DartCollector)  # bypass __init__ (no key / network)
    c.settings = None
    c.api_key = "test"
    c.dart = fake
    return c


def test_dart_ownership():
    c = _dart(_FakeDart(major=[
        {"rcept_dt": "2024.03.15", "repror": "국민연금공단", "report_tp": "변동",
         "stkqy": "12,345,678", "stkrt": "8.15"},
        {"rcept_dt": "2024.01.10", "repror": "", "stkqy": "1"},  # dropped: no holder
    ]))
    rows = c.fetch_ownership("005930")
    assert len(rows) == 1
    r = rows[0]
    assert r.holder_name == "국민연금공단"
    assert r.as_of_date == "2024-03-15"
    assert r.shares == 12345678.0
    assert r.pct == 8.15


def test_dart_insiders_txn_type_and_seq():
    c = _dart(_FakeDart(exec_=[
        {"rcept_no": "R1", "rcept_dt": "2024.05.02", "repror": "홍길동",
         "isu_exctv_ofcps": "대표이사", "sp_stock_lmp_irds_cnt": "1,000",
         "sp_stock_lmp_cnt": "5,000"},
        {"rcept_no": "R1", "rcept_dt": "2024.05.02", "repror": "홍길동",
         "isu_exctv_ofcps": "대표이사", "sp_stock_lmp_irds_cnt": "-500",
         "sp_stock_lmp_cnt": "4,500"},
        {"rcept_no": "R2", "rcept_dt": "2024.05.03", "repror": "김철수",
         "isu_exctv_ofcps": "감사", "sp_stock_lmp_irds_cnt": "0",
         "sp_stock_lmp_cnt": "100"},
    ]))
    rows = c.fetch_insiders("005930", "2024-01-01", "2024-12-31")
    assert len(rows) == 3
    r1 = [r for r in rows if r.filing_id == "R1"]
    assert {r.txn_seq for r in r1} == {0, 1}          # distinct seq within one filing
    assert {r.txn_type for r in r1} == {"buy", "sell"}
    buy = next(r for r in r1 if r.txn_type == "buy")
    assert buy.shares == 1000.0
    hold = next(r for r in rows if r.filing_id == "R2")
    assert hold.txn_type == "hold" and hold.shares == 100.0


def test_dart_insiders_date_filter():
    c = _dart(_FakeDart(exec_=[
        {"rcept_no": "R1", "rcept_dt": "2020.05.02", "repror": "홍길동",
         "sp_stock_lmp_irds_cnt": "1,000"},  # outside window
    ]))
    rows = c.fetch_insiders("005930", "2024-01-01", "2024-12-31")
    assert rows == []


# -------------------------------------------------------------------- KRX
def _krx():
    c = object.__new__(KrxCollector)  # bypass __init__ (no pykrx import/login)
    c.settings = None

    class _Stock:
        pass
    c.stock = _Stock()
    return c


def test_krx_foreign_ownership():
    c = _krx()
    df = pd.DataFrame(
        {"상장주식수": [5969782550], "지분율": [53.12], "한도소진율": [53.12]},
        index=["005930"])

    def _by_ticker(date, market, balance_limit=False):
        return df if market == "KOSPI" else pd.DataFrame()
    c.stock.get_exhaustion_rates_of_foreign_investment_by_ticker = _by_ticker

    rows = c.fetch_daily_metrics("005930", "2024-01-01", "2024-06-30")
    assert len(rows) == 1
    assert rows[0].foreign_own_pct == 53.12
    assert rows[0].shares_outstanding == 5969782550
    assert rows[0].metric_date == "2024-06-30"


def test_krx_foreign_ownership_needs_login():
    c = _krx()

    def _boom(date, market, balance_limit=False):
        raise RuntimeError("KRX 로그인 실패")
    c.stock.get_exhaustion_rates_of_foreign_investment_by_ticker = _boom

    try:
        c.fetch_daily_metrics("005930", "2024-01-01", "2024-06-30")
        raise AssertionError("expected NotSupportedError")
    except NotSupportedError:
        pass


def test_krx_index_membership():
    c = _krx()

    def _members(code, date=None, alternative=False):
        return ["005930", "000660"] if code == "1028" else ["035420"]
    c.stock.get_index_portfolio_deposit_file = _members

    rows = c.fetch_index_membership("005930")
    assert [r.index_name for r in rows] == ["KOSPI200"]  # in KOSPI200 only
