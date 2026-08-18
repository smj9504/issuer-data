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


FORM4_XML = b"""<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerName>Apple Inc.</issuerName></issuer>
  <reportingOwner><reportingOwnerId><rptOwnerName>WILLIAMS JEFFREY E</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>COO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable><nonDerivativeTransaction>
    <transactionShares><value>8384</value></transactionShares>
    <transactionPricePerShare><value>251.10</value></transactionPricePerShare>
    <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>"""


FACTS_STMT = {
    "cik": 320193,
    "facts": {"us-gaap": {
        "RevenueFromContract": {"label": "Revenue", "units": {"USD": [
            # duration (has start) → IS; two points same fy/fp, frame one must win
            {"start": "2022-10-01", "end": "2023-09-30", "val": 383, "fy": 2023, "fp": "FY",
             "form": "10-K", "filed": "2023-11-03", "frame": "CY2023"},
            {"start": "2022-10-01", "end": "2023-09-30", "val": 999, "fy": 2023, "fp": "FY",
             "form": "10-K", "filed": "2023-11-03"},
        ]}},
        "Assets": {"label": "Assets", "units": {"USD": [
            # instant (no start) → BS
            {"end": "2023-09-30", "val": 352, "fy": 2023, "fp": "FY", "form": "10-K",
             "filed": "2023-11-03"},
        ]}},
        "NetCashProvidedByUsedInOperatingActivities": {"label": "CFO", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 110, "fy": 2023, "fp": "FY",
             "form": "10-K", "filed": "2023-11-03"},
        ]}},
    }},
}


def test_financials_statement_type_and_dedup(monkeypatch):
    c = _collector(monkeypatch, FACTS_STMT)
    facts = c.fetch_financials("AAPL")
    by = {f.account: f for f in facts}
    assert by["RevenueFromContract"].statement_type == "IS"      # duration
    assert by["RevenueFromContract"].value == 383                 # frame point won (not 999)
    assert by["Assets"].statement_type == "BS"                    # instant
    assert by["NetCashProvidedByUsedInOperatingActivities"].statement_type == "CF"
    # dedup: exactly one Revenue row despite two source points
    assert sum(1 for f in facts if f.account == "RevenueFromContract") == 1


def test_parse_form4(monkeypatch):
    c = _collector(monkeypatch, {})
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: FORM4_XML)
    rows = c._parse_form4("http://x/form4.xml", "AAPL", "2024-12-18", "acc-1")
    assert len(rows) == 1
    r = rows[0]
    assert r.insider == "WILLIAMS JEFFREY E"
    assert r.relation == "COO"
    assert r.txn_type == "sell"
    assert r.shares == 8384.0
    assert r.price == 251.10


def test_form4_xml_url_strips_render_dir(monkeypatch):
    c = _collector(monkeypatch, {})
    url = c._form4_xml_url("https://sec.gov/x/000/acc", "xslF345X06/form4.xml")
    assert url == "https://sec.gov/x/000/acc/form4.xml"  # render-dir stripped
