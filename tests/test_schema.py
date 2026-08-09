def test_schema_creates_tables_and_views(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {
        "companies", "securities", "identifier_xref", "prices", "financials",
        "filings", "filing_documents", "fx_rates", "company_peers", "collection_runs",
    }
    assert expected <= tables

    views = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    )}
    assert {"v_latest_price", "v_company_overview"} <= views
