"""Offline test: Alpha Vantage derives distinct Q1..Q4 (regression for the 'Q' collapse)."""

from issuer_data.collectors.alpha_vantage import AlphaVantageCollector, _quarter_of
from issuer_data.config import Settings


def test_quarter_of():
    assert _quarter_of("2023-03-31") == "Q1"
    assert _quarter_of("2023-06-30") == "Q2"
    assert _quarter_of("2023-09-30") == "Q3"
    assert _quarter_of("2023-12-31") == "Q4"


INCOME = {
    "symbol": "AAPL",
    "quarterlyReports": [
        {"fiscalDateEnding": "2023-03-31", "reportedCurrency": "USD", "totalRevenue": "100"},
        {"fiscalDateEnding": "2023-06-30", "reportedCurrency": "USD", "totalRevenue": "200"},
        {"fiscalDateEnding": "2023-09-30", "reportedCurrency": "USD", "totalRevenue": "300"},
        {"fiscalDateEnding": "2023-12-31", "reportedCurrency": "USD", "totalRevenue": "400"},
    ],
    "annualReports": [
        {"fiscalDateEnding": "2023-12-31", "reportedCurrency": "USD", "totalRevenue": "1000"},
    ],
}


def test_quarters_do_not_collapse(monkeypatch):
    c = AlphaVantageCollector(Settings(alphavantage_api_key="test"))
    # every statement function returns the same income fixture; keep it simple
    monkeypatch.setattr(c, "_get", lambda function, **kw: INCOME)
    facts = [f for f in c.fetch_financials("AAPL") if f.account == "totalRevenue"]
    periods = {f.fiscal_period for f in facts}
    assert {"Q1", "Q2", "Q3", "Q4", "FY"} <= periods  # 4 distinct quarters, not one "Q"
    # each quarter kept a distinct value
    byp = {f.fiscal_period: f.value for f in facts}
    assert byp["Q1"] == 100 and byp["Q4"] == 400 and byp["FY"] == 1000
