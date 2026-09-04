"""Offline tests for the Extension-B/C coverage layer and correctness fixes."""

from issuer_data.config import Settings
from issuer_data.models import (
    Company,
    DailyMetric,
    FinancialFact,
    InsiderTrade,
    Security,
)
from issuer_data.orchestrator import Orchestrator
from issuer_data.services import _latest_metric
from issuer_data.storage.repository import Repository


def _company(repo, cid_name="ACME"):
    cid = repo.resolve_company(Company(name=cid_name, cik="111", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="ACME", currency="USD", source="edgar"), cid)
    return cid


def test_insider_idempotent_and_multi_txn(conn):
    repo = Repository(conn)
    _company(repo)
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


def test_recollecting_a_row_updates_its_values(conn):
    """A second collection of the same key must refresh the row, not skip it.

    The upsert is one generic statement shared by every coverage table, so the
    conflict clause is written once and applies to all of them. Getting it
    wrong in the direction of "leave the existing row alone" is invisible: the
    row count stays right, nothing errors, and every re-collection quietly
    keeps the first value it ever saw. Counting rows cannot see that, so this
    asserts on a value.
    """
    repo = Repository(conn)
    _company(repo)

    def _trade(shares, price):
        return InsiderTrade(symbol="ACME", market="US", filed_date="2024-11-07",
                            insider="DOE JANE", txn_type="sell", txn_seq=0,
                            shares=shares, price=price, filing_id="acc1", source="edgar")

    repo.upsert_coverage("insider_trades", "company", [_trade(100, 10.0)])
    repo.commit()
    # Same primary key, restated figures — what an amended filing looks like.
    repo.upsert_coverage("insider_trades", "company", [_trade(250, 12.5)])
    repo.commit()

    rows = conn.execute("SELECT shares, price FROM insider_trades").fetchall()
    assert len(rows) == 1, "the restated row should replace, not accumulate"
    assert rows[0]["shares"] == 250, "re-collection left a stale value behind"
    assert rows[0]["price"] == 12.5


def test_recollecting_a_security_grain_row_updates_its_values(conn):
    """The same guarantee on the security-grain branch of the upsert."""
    repo = Repository(conn)
    _company(repo)

    def _metric(foreign_own_pct):
        return DailyMetric(symbol="ACME", market="US", metric_date="2026-01-05",
                           foreign_own_pct=foreign_own_pct, source="krx")

    repo.upsert_coverage("daily_metrics", "security", [_metric(51.2)])
    repo.commit()
    repo.upsert_coverage("daily_metrics", "security", [_metric(48.7)])
    repo.commit()

    rows = conn.execute("SELECT foreign_own_pct FROM daily_metrics").fetchall()
    assert len(rows) == 1
    assert rows[0]["foreign_own_pct"] == 48.7, "re-collection left a stale value behind"


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


def test_unimplemented_coverage_type_is_skipped(conn):
    # yfinance implements no fetch_ratios -> the run must record 'skipped', never
    # 'ok' with 0 rows (the base-collector override probe short-circuits up front).
    orch = Orchestrator(conn, Settings())
    orch.collect_coverage("US", "ratios", source="yfinance",
                          symbols=["AAPL"], start=None, end=None)
    status = conn.execute(
        "SELECT status FROM collection_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()[0]
    assert status == "skipped"
