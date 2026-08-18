"""Offline tests for the Extension-B/C coverage layer and correctness fixes."""

from issuer_data.models import Company, FinancialFact, InsiderTrade, Security
from issuer_data.services import _latest_metric
from issuer_data.storage.repository import Repository


def _company(repo, cid_name="ACME"):
    cid = repo.resolve_company(Company(name=cid_name, cik="111", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="ACME", currency="USD", source="edgar"), cid)
    return cid


def test_insider_idempotent_and_multi_txn(conn):
    repo = _company_repo = Repository(conn)
    cid = _company(repo)
    # two transactions in one filing on the same day, same acquired/disposed code
    rows = [
        InsiderTrade(symbol="ACME", market="US", filed_date="2024-11-07", insider="DOE JANE",
                     txn_type="sell", txn_seq=0, shares=100, price=10.0, filing_id="acc1", source="edgar"),
        InsiderTrade(symbol="ACME", market="US", filed_date="2024-11-07", insider="DOE JANE",
                     txn_type="sell", txn_seq=1, shares=50, price=11.0, filing_id="acc1", source="edgar"),
        InsiderTrade(symbol="ACME", market="US", filed_date="2024-11-08", insider="DOE JANE",
                     txn_type="hold", txn_seq=0, filing_id="acc2", source="edgar"),
    ]
    repo.upsert_coverage("insider_trades", "company", rows)
    repo.commit()
    n1 = conn.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0]
    assert n1 == 3  # both txns kept + the holding row
    # re-run must not duplicate (holding row previously duplicated due to NULL PK)
    repo.upsert_coverage("insider_trades", "company", rows)
    repo.commit()
    assert conn.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0] == 3


def test_latest_metric_prefers_cfs_fy(conn):
    repo = Repository(conn)
    cid = _company(repo)
    facts = [
        FinancialFact(symbol="ACME", market="US", fiscal_year=2023, fiscal_period="FY",
                      fs_scope="CFS", statement_type="IS", account="Revenues", value=1000.0,
                      currency="USD", source="edgar"),
        FinancialFact(symbol="ACME", market="US", fiscal_year=2023, fiscal_period="Q4",
                      fs_scope="CFS", statement_type="IS", account="Revenues", value=300.0,
                      currency="USD", source="edgar"),
        FinancialFact(symbol="ACME", market="US", fiscal_year=2023, fiscal_period="FY",
                      fs_scope="OFS", statement_type="IS", account="Revenues", value=800.0,
                      currency="USD", source="edgar"),
    ]
    repo.upsert_financials(cid, facts)
    repo.commit()
    # must pick the CFS + FY figure (1000), not the quarterly (300) or separate (800)
    assert _latest_metric(conn, cid, ("Revenues",)) == 1000.0
