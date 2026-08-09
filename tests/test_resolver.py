from issuer_data.collectors.resolver import apply_overrides, resolve_and_store
from issuer_data.models import SecurityRecord
from issuer_data.storage.repository import Repository


def test_adr_links_to_underlying_by_isin(conn):
    repo = Repository(conn)
    records = [
        SecurityRecord(market="HK", symbol="09988", name="BABA-SW", isin="ISIN_BABA",
                       country="CN", security_type="COMMON", currency="HKD", source="hkexnews"),
        SecurityRecord(market="US", symbol="BABA", name="Alibaba ADR", security_type="ADR",
                       currency="USD", underlying_isin="ISIN_BABA", source="fmp"),
    ]
    mapping = resolve_and_store(repo, records)
    assert mapping["HK:09988"] == mapping["US:BABA"]  # one company, two listings
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_overrides_merge_listings(conn, tmp_path):
    repo = Repository(conn)
    # two separate companies first
    resolve_and_store(repo, [
        SecurityRecord(market="US", symbol="BABA", name="Alibaba ADR",
                       security_type="ADR", currency="USD", source="fmp"),
        SecurityRecord(market="HK", symbol="09988", name="BABA-SW",
                       security_type="COMMON", currency="HKD", source="hkexnews"),
    ])
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2

    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text("group,market,symbol\nalibaba,US,BABA\nalibaba,HK,9988\n", encoding="utf-8")
    apply_overrides(repo, csv_path)

    cids = {r[0] for r in conn.execute(
        "SELECT company_id FROM securities WHERE symbol IN ('BABA','09988')"
    )}
    assert len(cids) == 1  # merged onto one company
