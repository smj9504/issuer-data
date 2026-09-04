def test_schema_creates_tables_and_views(conn):
    tables = {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )}
    expected = {
        "companies", "securities", "identifier_xref", "prices", "financials",
        "filings", "filing_documents", "fx_rates", "company_peers", "collection_runs",
    }
    assert expected <= tables

    views = {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.views WHERE table_schema = 'public'"
    )}
    assert {"v_latest_price", "v_company_overview"} <= views
