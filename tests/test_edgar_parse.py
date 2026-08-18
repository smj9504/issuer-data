"""Offline test: EdgarCollector parses companyfacts/submissions JSON into models."""

import pytest

from issuer_data.collectors.us_edgar import (
    EDGAR_FULLTEXT_URL,
    NASDAQ_IPO_CALENDAR_URL,
    SUBMISSIONS_URL,
    EdgarCollector,
)
from issuer_data.config import Settings
from issuer_data.models import Company, Filing, Security
from issuer_data.storage.repository import Repository

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


# --- demand signals ----------------------------------------------------------

S1A_HTML = b"<html><body>The offering price ... will be between $18.00 and $20.00 per share.</body></html>"
PROSPECTUS_HTML = b"<html><body>The initial public offering price is $22.00 per share.</body></html>"

ANCHOR_HTML_ONE = (
    b"<html><body>Entities affiliated with Acme Capital have indicated an "
    b"interest in purchasing up to $50 million of shares of our common "
    b"stock in this offering at the initial public offering price.</body></html>"
)
ANCHOR_HTML_TWO = (
    b"<html><body>Entities affiliated with Acme Capital have indicated an "
    b"interest in purchasing up to $50 million of shares. Separately, funds "
    b"affiliated with Beta Partners have indicated an interest in purchasing "
    b"up to $30 million of shares, in each case at the initial public "
    b"offering price.</body></html>"
)

EFTS_HITS = {
    "hits": {
        "hits": [
            {
                "_id": "0000320193-24-000050:exhibit991.htm",
                "_source": {
                    "ciks": ["0000320193"],
                    "form": "8-K",
                    "file_description": "Press Release",
                    "file_date": "2024-03-01",
                },
            }
        ]
    }
}


def test_price_band_signal_reads_range_and_final_price(monkeypatch):
    c = _collector(monkeypatch, {})
    docs = {"https://x/s1a.htm": S1A_HTML, "https://x/424b4.htm": PROSPECTUS_HTML}
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: docs[url])
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2024-01-10",
               filing_type="S-1/A", url="https://x/s1a.htm", source="edgar"),
        Filing(symbol="AAPL", market="US", filing_id="2", filed_date="2024-02-01",
               filing_type="424B4", url="https://x/424b4.htm", source="edgar"),
    ]
    out = c._price_band_signal("AAPL", filings)
    assert len(out) == 1
    sig = out[0]
    assert sig.signal_type == "price_band"
    assert sig.price == 22.0
    assert "18.00" in sig.detail and "20.00" in sig.detail


def test_price_band_signal_empty_when_nothing_found(monkeypatch):
    c = _collector(monkeypatch, {})
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: b"<html>nothing here</html>")
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2024-01-10",
               filing_type="S-1", url="https://x/s1.htm", source="edgar"),
    ]
    assert c._price_band_signal("AAPL", filings) == []


def test_anchor_investor_signal_dedups_repeated_amendment(monkeypatch):
    c = _collector(monkeypatch, {})
    docs = {"https://x/s1a1.htm": ANCHOR_HTML_ONE, "https://x/s1a2.htm": ANCHOR_HTML_ONE}
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: docs[url])
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2024-01-10",
               filing_type="S-1/A", url="https://x/s1a1.htm", source="edgar"),
        Filing(symbol="AAPL", market="US", filing_id="2", filed_date="2024-01-20",
               filing_type="S-1/A", url="https://x/s1a2.htm", source="edgar"),
    ]
    out = c._anchor_investor_signal("AAPL", filings)
    assert len(out) == 1  # same disclosure repeated verbatim -> deduped to one row
    assert "acme capital" in out[0].investor_name.lower()
    assert out[0].indicated_amount == 50_000_000.0


def test_anchor_investor_signal_keeps_two_investors_same_filing(monkeypatch):
    c = _collector(monkeypatch, {})
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: ANCHOR_HTML_TWO)
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2024-01-10",
               filing_type="424B4", url="https://x/424b4.htm", source="edgar"),
    ]
    out = c._anchor_investor_signal("AAPL", filings)
    assert len(out) == 2
    assert sorted(s.indicated_amount for s in out) == [30_000_000.0, 50_000_000.0]
    assert len({s.signal_date for s in out}) == 1  # same filing -> same signal_date
    assert len({s.investor_name for s in out}) == 2  # but distinct investor_name


ANCHOR_HTML_WITH_CONTEXT = (
    b"<html><body>Prior to this offering, Acme Capital held an indirect interest "
    b"in the Company through an investment vehicle. Entities affiliated with Acme "
    b"Capital have indicated an interest in purchasing up to $50 million of shares "
    b"of our common stock in this offering at the initial public offering price. "
    b"Following this offering, Acme Capital is expected to hold approximately 3% "
    b"of our outstanding common stock.</body></html>"
)


def test_anchor_investor_signal_excerpt_includes_surrounding_context(monkeypatch):
    """Real filings put relevant detail (existing stake conversions,
    resulting post-IPO ownership %) in sentences around the regex match,
    not inside it — `detail` must not be limited to the bare matched
    sentence.
    """
    c = _collector(monkeypatch, {})
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: ANCHOR_HTML_WITH_CONTEXT)
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2024-01-10",
               filing_type="424B4", url="https://x/424b4.htm", source="edgar"),
    ]
    out = c._anchor_investor_signal("AAPL", filings)
    assert len(out) == 1
    detail = out[0].detail
    assert "indicated an interest in purchasing" in detail  # the match itself
    assert "indirect interest" in detail  # context before the match
    assert "approximately 3%" in detail  # context after the match


def test_anchor_investor_signals_both_persist_via_upsert_coverage(monkeypatch, conn):
    """Regression test for the PK widening: two distinct investors disclosed
    in the same filing share (company_id, signal_date, signal_type, source)
    — only the added `investor_name` column keeps them from colliding under
    Repository.upsert_coverage's INSERT OR REPLACE.
    """
    c = _collector(monkeypatch, {})
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: ANCHOR_HTML_TWO)
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2024-01-10",
               filing_type="424B4", url="https://x/424b4.htm", source="edgar"),
    ]
    signals = c._anchor_investor_signal("AAPL", filings)
    assert len(signals) == 2  # sanity check on the fixture itself

    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="Apple Inc.", cik="0000320193", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="AAPL", currency="USD", source="edgar"), cid)
    n = repo.upsert_coverage("demand_signals", "company", signals)
    assert n == 2
    rows = conn.execute("SELECT investor_name FROM demand_signals").fetchall()
    assert len(rows) == 2  # neither row silently overwrote the other


def test_confidential_review_signal_computes_gap():
    c = EdgarCollector(Settings(sec_user_agent="test test@example.com"))
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2023-11-01",
               filing_type="DRS", url="https://x/drs1.htm", source="edgar"),
        Filing(symbol="AAPL", market="US", filing_id="2", filed_date="2023-12-01",
               filing_type="DRS/A", url="https://x/drs2.htm", source="edgar"),
        Filing(symbol="AAPL", market="US", filing_id="3", filed_date="2024-01-15",
               filing_type="S-1", url="https://x/s1.htm", source="edgar"),
        Filing(symbol="AAPL", market="US", filing_id="4", filed_date="2024-01-25",
               filing_type="S-1/A", url="https://x/s1a.htm", source="edgar"),
    ]
    out = c._confidential_review_signal("AAPL", filings)
    assert len(out) == 1
    sig = out[0]
    assert sig.signal_type == "confidential_review"
    assert sig.signal_date == "2024-01-15"  # first public S-1, not the amendment
    assert "75 days" in sig.detail
    assert "2 draft submission" in sig.detail


def test_confidential_review_signal_ignores_undated_drs():
    c = EdgarCollector(Settings(sec_user_agent="test test@example.com"))
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date=None,
               filing_type="DRS", url="https://x/drs1.htm", source="edgar"),
        Filing(symbol="AAPL", market="US", filing_id="2", filed_date="2024-01-15",
               filing_type="S-1", url="https://x/s1.htm", source="edgar"),
    ]
    assert c._confidential_review_signal("AAPL", filings) == []  # no dated DRS -> no crash


def test_confidential_review_signal_covers_foreign_private_issuer():
    """Foreign private issuers file F-1/F-1-A instead of S-1/S-1-A as their
    public registration statement (e.g. real-world Birkenstock/BIRK) — this
    must not silently return empty just because the reveal filing isn't S-1.
    """
    c = EdgarCollector(Settings(sec_user_agent="test test@example.com"))
    filings = [
        Filing(symbol="BIRK", market="US", filing_id="1", filed_date="2023-06-01",
               filing_type="DRS", url="https://x/drs1.htm", source="edgar"),
        Filing(symbol="BIRK", market="US", filing_id="2", filed_date="2023-09-08",
               filing_type="F-1", url="https://x/f1.htm", source="edgar"),
    ]
    out = c._confidential_review_signal("BIRK", filings)
    assert len(out) == 1
    sig = out[0]
    assert sig.signal_date == "2023-09-08"
    assert "first public F-1 2023-09-08" in sig.detail


def test_registration_and_424b_filings_includes_f1(monkeypatch):
    c = _collector(monkeypatch, {})
    filings = [
        Filing(symbol="BIRK", market="US", filing_id="1", filed_date="2023-09-08",
               filing_type="F-1", url="https://x/f1.htm", source="edgar"),
        Filing(symbol="BIRK", market="US", filing_id="2", filed_date="2023-09-20",
               filing_type="F-1/A", url="https://x/f1a.htm", source="edgar"),
        Filing(symbol="BIRK", market="US", filing_id="3", filed_date="2023-10-12",
               filing_type="424B4", url="https://x/424b4.htm", source="edgar"),
    ]
    regs, prospectuses = c._registration_and_424b_filings(filings)
    assert [f.filing_type for f in regs] == ["F-1", "F-1/A"]
    assert [f.filing_type for f in prospectuses] == ["424B4"]


def test_confidential_review_signal_no_drs_returns_empty():
    c = EdgarCollector(Settings(sec_user_agent="test test@example.com"))
    filings = [
        Filing(symbol="AAPL", market="US", filing_id="1", filed_date="2024-01-15",
               filing_type="S-1", url="https://x/s1.htm", source="edgar"),
    ]
    assert c._confidential_review_signal("AAPL", filings) == []


def test_edgar_fulltext_demand_signal(monkeypatch):
    c = _collector(monkeypatch, EFTS_HITS)
    out = c._edgar_fulltext_demand_signal("AAPL", "0000320193", ipo_date="2024-02-15")
    assert len(out) == 1
    sig = out[0]
    assert sig.signal_type == "sec_fulltext"
    assert sig.signal_date == "2024-03-01"
    assert 'mentions "oversubscribed"' in sig.detail
    assert sig.url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000050/exhibit991.htm"
    )


def test_ttw_fulltext_signal(monkeypatch):
    c = _collector(monkeypatch, EFTS_HITS)
    out = c._ttw_fulltext_signal("AAPL", "0000320193")
    assert len(out) == 1
    assert out[0].signal_type == "ttw_fulltext"
    assert 'mentions "testing-the-waters"' in out[0].detail


EFTS_HITS_WITH_EXHIBIT = {
    "hits": {
        "hits": [
            {
                "_id": "0000320193-24-000050:exhibit991.htm",
                "_source": {
                    "ciks": ["0000320193"],
                    "form": "8-K",
                    "file_description": "Press Release",
                    "file_date": "2024-03-01",
                },
            },
            {
                "_id": "0000320193-24-000051:exhibit11.htm",
                "_source": {
                    "ciks": ["0000320193"],
                    "form": "S-1/A",
                    "file_type": "EX-1.1",
                    "file_description": "EX-1.1",
                    "file_date": "2024-02-15",
                },
            },
        ]
    }
}

# Real EFTS hits always carry both `file_type` and `file_description` set to
# "EX-1.1" (confirmed via a live query), but older/inconsistent filings may
# only have one — this fixture has `file_type` missing to exercise the
# fallback to `file_description`.
EFTS_HITS_EXHIBIT_FILE_TYPE_MISSING = {
    "hits": {
        "hits": [
            {
                "_id": "0000320193-24-000052:exhibit11-legacy.htm",
                "_source": {
                    "ciks": ["0000320193"],
                    "form": "S-1/A",
                    "file_description": "EX-1.1",
                    "file_date": "2024-02-20",
                },
            },
        ]
    }
}


def test_ttw_fulltext_signal_excludes_underwriting_exhibit(monkeypatch):
    """Empirically (5 real 2023-2024 IPOs, plus a live 283-hit EFTS query),
    every "testing-the-waters" hit was the underwriting agreement's
    boilerplate Rule 163B representation, filed as Exhibit 1.1 — excluding
    it is what keeps this signal selective.
    """
    c = _collector(monkeypatch, EFTS_HITS_WITH_EXHIBIT)
    out = c._ttw_fulltext_signal("AAPL", "0000320193")
    assert len(out) == 1  # EX-1.1 hit filtered out
    assert out[0].signal_date == "2024-03-01"


def test_ttw_fulltext_signal_exhibit_filter_falls_back_to_file_description(monkeypatch):
    """`file_type` is EDGAR's structured exhibit classification and is
    preferred, but some hits only carry `file_description` — the filter
    must still catch those.
    """
    c = _collector(monkeypatch, EFTS_HITS_EXHIBIT_FILE_TYPE_MISSING)
    out = c._ttw_fulltext_signal("AAPL", "0000320193")
    assert out == []  # excluded via the file_description fallback


def test_edgar_fulltext_demand_signal_keeps_underwriting_exhibit(monkeypatch):
    """The exhibit filter is opt-in (`skip_underwriting_exhibits`) and must
    not affect the unrelated "oversubscribed" signal.
    """
    c = _collector(monkeypatch, EFTS_HITS_WITH_EXHIBIT)
    out = c._edgar_fulltext_demand_signal("AAPL", "0000320193")
    assert len(out) == 2


def test_fetch_demand_signals_isolates_failures(monkeypatch):
    """One sub-signal (EFTS-backed) raising must not blank out the rest of
    fetch_demand_signals's result.
    """
    c = EdgarCollector(Settings(sec_user_agent="test test@example.com"))
    c._ticker_map = {"AAPL": {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"}}

    def fake_get_json(url, **kw):
        if url == SUBMISSIONS_URL.format(cik10="0000320193"):
            return SUBMISSIONS
        if url == EDGAR_FULLTEXT_URL:
            raise RuntimeError("EFTS down")
        if url == NASDAQ_IPO_CALENDAR_URL:
            return {"data": {}}
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(c.client, "get_json", fake_get_json)
    out = c.fetch_demand_signals("AAPL")  # must not raise
    assert out == []


def test_base_collector_demand_signals_not_supported():
    from issuer_data.collectors.base import BaseCollector, NotSupportedError

    class DummyCollector(BaseCollector):
        source = "dummy"

    with pytest.raises(NotSupportedError):
        DummyCollector().fetch_demand_signals("X")


SC13G_HTML = b"""<html><body>
<p>SCHEDULE 13G</p>
<table>
<tr><td>NAME OF REPORTING PERSONS</td></tr>
<tr><td>I.R.S. IDENTIFICATION NO. OF ABOVE PERSON (ENTITIES ONLY)</td></tr>
<tr><td>The Vanguard Group</td></tr>
<tr><td>AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON</td></tr>
<tr><td>1,234,567</td></tr>
<tr><td>PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11)</td></tr>
<tr><td>8.4%</td></tr>
</table>
</body></html>"""


def test_parse_13dg_cover_page(monkeypatch):
    c = _collector(monkeypatch, {})
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: SC13G_HTML)
    row = c._parse_13dg("http://x/sc13g.htm", "AAPL", "2024-02-14", "acc-9", "SC 13G")
    assert row is not None
    assert row.holder_name == "The Vanguard Group"
    assert row.pct == 8.4
    assert row.shares == 1234567.0
    assert row.holder_type == "SC 13G"


def test_parse_13dg_skips_empty(monkeypatch):
    c = _collector(monkeypatch, {})
    monkeypatch.setattr(c.client, "get_bytes", lambda url, **kw: b"<html><body>nothing</body></html>")
    assert c._parse_13dg("http://x/x.htm", "AAPL", "2024-01-01", "a", "SC 13D") is None
