"""Offline test: EdgarCollector parses companyfacts/submissions JSON into models."""

from issuer_data.config import Settings
from issuer_data.collectors.us_edgar import EdgarCollector

COMPANYFACTS = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {"end": "2023-09-30", "val": 383285000000, "fy": 2023, "fp": "FY", "form": "10-K"},
                        {"end": "2022-09-30", "val": 394328000000, "fy": 2022, "fp": "FY", "form": "10-K"},
                        {"end": "2023-06-30", "val": 0, "fy": 2023, "fp": "Q3", "form": "8-K"},  # dropped
                    ]
                },
            }
        }
    },
}

SUBMISSIONS = {
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-23-000106", "0000320193-24-000001"],
            "filingDate": ["2023-11-03", "2024-01-15"],
            "form": ["10-K", "8-K"],
            "primaryDocument": ["aapl-20230930.htm", "a8k.htm"],
            "primaryDocDescription": ["10-K", "8-K"],
        }
    }
}


def _collector(monkeypatch, payload):
    c = EdgarCollector(Settings(sec_user_agent="test test@example.com"))
    c._ticker_map = {"AAPL": {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"}}
    monkeypatch.setattr(c.client, "get_json", lambda url, **kw: payload)
    return c


def test_fetch_financials_keeps_periodic_forms(monkeypatch):
    c = _collector(monkeypatch, COMPANYFACTS)
    facts = c.fetch_financials("AAPL")
    # only 10-K rows kept (8-K dropped)
    assert len(facts) == 2
    assert {f.fiscal_year for f in facts} == {2022, 2023}
    f = next(f for f in facts if f.fiscal_year == 2023)
    assert f.account == "Revenues"
    assert f.value == 383285000000
    assert f.currency == "USD"
    assert f.market == "US"


def test_fetch_filings_builds_urls_and_filters_dates(monkeypatch):
    c = _collector(monkeypatch, SUBMISSIONS)
    filings = c.fetch_filings("AAPL", "2023-01-01", "2023-12-31")
    assert len(filings) == 1  # 2024 filing excluded by date range
    fl = filings[0]
    assert fl.filing_type == "10-K"
    assert fl.filing_id == "0000320193-23-000106"
    assert fl.url.endswith("/000032019323000106/aapl-20230930.htm")
    assert fl.doc_urls
