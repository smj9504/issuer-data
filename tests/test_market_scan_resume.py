"""Full-market sweep: resumability, daily call budget, and 원문 re-fetch avoidance.

A DART sweep over ~2,600 KR listings is thousands of metered calls and will not
finish inside one day's quota. The three things that make it usable are fixed
here:

  1. progress is committed per symbol, so a stopped run resumes instead of
     restarting (restarting would re-spend a whole day's quota on stored data);
  2. the day's call count lives in the database, so a re-run inherits it rather
     than getting a fresh allowance the provider will not honour;
  3. an already-stored 접수번호 is never re-opened — the single largest source
     of avoidable calls on a repeat sweep.
"""

import pytest

from issuer_data.collectors.base import (
    BaseCollector,
    CallBudget,
    NotSupportedError,
    QuotaExceededError,
)
from issuer_data.config import Settings
from issuer_data.models import StakeChange
from issuer_data.orchestrator import Orchestrator
from issuer_data.storage.repository import Repository

SCOPE = ("KR", "stake", "dart", "2026-01-01", "2026-08-31")


@pytest.fixture()
def repo(conn) -> Repository:
    return Repository(conn)


def _seed_securities(conn, symbols):
    """Minimal company+security rows so symbol -> company_id resolves."""
    for sym in symbols:
        cur = conn.execute(
            "INSERT INTO companies(name, country, source) VALUES (%s,%s,%s) "
            "RETURNING company_id",
            (sym, "KR", "test"),
        )
        conn.execute(
            "INSERT INTO securities(company_id, market, symbol, source) VALUES (%s,%s,%s,%s)",
            (cur.fetchone()["company_id"], "KR", sym, "test"),
        )
    conn.commit()


# --------------------------------------------------------------- scan cursor
def test_done_symbols_are_remembered_and_errors_are_not(repo):
    """A failed symbol must be retried next run; only successes are skipped."""
    repo.mark_scan_symbol(*SCOPE, "005930", "done", rows=32)
    repo.mark_scan_symbol(*SCOPE, "000660", "error", error="timeout")
    assert repo.scan_done_symbols(*SCOPE) == {"005930"}


def test_cursor_is_scoped_to_the_date_range(repo):
    """A symbol finished for 2025 is not finished for a 2026 sweep.

    Without the range in the key, widening --start would silently report the
    whole market as already collected and return nothing.
    """
    repo.mark_scan_symbol("KR", "stake", "dart", "2025-01-01", "2025-12-31",
                          "005930", "done", rows=5)
    assert repo.scan_done_symbols(*SCOPE) == set()
    assert repo.scan_done_symbols("KR", "stake", "dart",
                                  "2025-01-01", "2025-12-31") == {"005930"}


def test_clearing_one_scope_leaves_the_others_intact(repo):
    repo.mark_scan_symbol(*SCOPE, "005930", "done", rows=1)
    repo.mark_scan_symbol("KR", "treasury", "dart", "2026-01-01", "2026-08-31",
                          "005930", "done", rows=1)
    repo.clear_scan_progress(*SCOPE)
    assert repo.scan_done_symbols(*SCOPE) == set()
    assert repo.scan_done_symbols("KR", "treasury", "dart",
                                  "2026-01-01", "2026-08-31") == {"005930"}


def test_marking_a_symbol_twice_updates_rather_than_duplicates(repo):
    repo.mark_scan_symbol(*SCOPE, "005930", "error", error="timeout")
    repo.mark_scan_symbol(*SCOPE, "005930", "done", rows=7, api_calls=2)
    summary = repo.scan_summary(*SCOPE)
    assert summary["done"] == 1 and summary["error"] == 0
    assert summary["rows_written"] == 7 and summary["api_calls"] == 2


# ------------------------------------------------------------- call budget
def test_budget_survives_a_process_restart(repo):
    """The whole point of storing the tally: a re-run must not get a fresh
    allowance the provider will not honour."""
    first = CallBudget(repo, "dart", limit=10, flush_every=1)
    for _ in range(8):
        first.spend()
    second = CallBudget(repo, "dart", limit=10)     # a new process, same day
    assert second.used == 8
    assert second.remaining == 2


def test_budget_refuses_the_call_that_would_exceed_the_limit(repo):
    budget = CallBudget(repo, "dart", limit=3, flush_every=1)
    for _ in range(3):
        budget.spend()
    with pytest.raises(QuotaExceededError, match="daily API budget exhausted"):
        budget.spend()
    # and the refused call was not charged
    assert repo.api_calls_today("dart") == 3


def test_budget_flushes_pending_calls_before_raising(repo):
    """Calls made since the last flush must reach the database, or the next run
    starts with an under-count and overshoots the real quota."""
    budget = CallBudget(repo, "dart", limit=5, flush_every=100)  # never auto-flushes
    for _ in range(5):
        budget.spend()
    assert repo.api_calls_today("dart") == 0        # still buffered
    with pytest.raises(QuotaExceededError):
        budget.spend()
    assert repo.api_calls_today("dart") == 5        # flushed on the way out


def test_budget_is_tracked_per_source_and_per_day(repo):
    repo.record_api_calls("dart", 5, day="2026-09-03")
    repo.record_api_calls("dart", 3, day="2026-09-04")
    repo.record_api_calls("krx", 7, day="2026-09-03")
    assert repo.api_calls_today("dart", "2026-09-03") == 5
    assert repo.api_calls_today("dart", "2026-09-04") == 3
    assert repo.api_calls_today("krx", "2026-09-03") == 7


# --------------------------------------------------- known 접수번호 skipping
def test_known_rcept_nos_reads_back_what_was_stored(repo, conn):
    _seed_securities(conn, ["005930"])
    cid = repo.get_company_id_for_symbol("KR", "005930")
    conn.execute(
        "INSERT INTO kr_stake_changes(company_id, rcept_no, change_date, holder_name, "
        "holder_id, source) VALUES (%s,%s,%s,%s,%s,%s)",
        (cid, "20260828001916", "2026-07-29", "삼성생명보험", "104-81-26688", "dart"),
    )
    conn.commit()
    assert repo.known_rcept_nos("kr_stake_changes", cid, "dart") == {"20260828001916"}
    # a different source's rows are not this source's evidence
    assert repo.known_rcept_nos("kr_stake_changes", cid, "other") == set()


def test_known_rcept_nos_rejects_a_table_without_that_key(repo):
    """The table name is interpolated into SQL, so it is allow-listed."""
    with pytest.raises(ValueError, match="not keyed by rcept_no"):
        repo.known_rcept_nos("prices", 1, "dart")


# ---------------------------------------------------- orchestrator sweep
class _FakeDart(BaseCollector):
    """A DART-shaped collector: counts calls, honours a budget, skips seen 접수번호."""

    source = "dart"
    market = "KR"

    def __init__(self, per_symbol=None, fail_on=(), calls_per_symbol=2):
        self.per_symbol = per_symbol or {}
        self.fail_on = set(fail_on)
        self.calls_per_symbol = calls_per_symbol
        self.api_calls = 0
        self.budget = None
        self.seen_rcept_nos: set[str] = set()
        self.visited: list[str] = []
        self.seen_arg: list[set[str]] = []

    def _spend(self, n=1):
        if self.budget is not None:
            self.budget.spend(n)
        self.api_calls += n

    def fetch_stake_changes(self, symbol, start, end):
        self.visited.append(symbol)
        self.seen_arg.append(set(self.seen_rcept_nos))
        self._spend(self.calls_per_symbol)
        if symbol in self.fail_on:
            raise RuntimeError("boom")
        return self.per_symbol.get(symbol, [])


def _change(symbol, rcept):
    return StakeChange(symbol=symbol, market="KR", source="dart", rcept_no=rcept,
                       change_date="2026-07-29", holder_name="삼성생명보험",
                       holder_id="104-81-26688", shares_delta=-247.0)


def _orchestrator(conn, collector, monkeypatch):
    from issuer_data import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "build_collector", lambda src, settings: collector)
    monkeypatch.setattr(orch_mod, "default_source", lambda market, dt: "dart")
    orch = Orchestrator(conn, Settings())
    monkeypatch.setattr(orch, "ensure_master", lambda market, syms: None)
    return orch


def test_sweep_covers_every_stored_symbol_when_none_are_named(conn, monkeypatch):
    _seed_securities(conn, ["000660", "005930", "035720"])
    collector = _FakeDart(per_symbol={"005930": [_change("005930", "R1")]})
    orch = _orchestrator(conn, collector, monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    assert collector.visited == ["000660", "005930", "035720"]


def test_resume_skips_completed_symbols_only(conn, monkeypatch):
    _seed_securities(conn, ["000660", "005930", "035720"])
    collector = _FakeDart(fail_on={"000660"})
    orch = _orchestrator(conn, collector, monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    assert collector.visited == ["000660", "005930", "035720"]

    # Second pass: 005930/035720 succeeded, 000660 failed and must be retried.
    collector.visited.clear()
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31",
                          resume=True)
    assert collector.visited == ["000660"]


def test_without_resume_every_symbol_is_visited_again(conn, monkeypatch):
    """--resume is opt-in: the default stays a plain re-collect, so a refresh of
    an already-swept range is still possible."""
    _seed_securities(conn, ["000660", "005930"])
    collector = _FakeDart()
    orch = _orchestrator(conn, collector, monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    collector.visited.clear()
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    assert collector.visited == ["000660", "005930"]


def test_quota_stops_the_sweep_and_leaves_the_cursor_usable(conn, monkeypatch):
    """Hitting the cap must end the run cleanly — the symbols already done stay
    done, and the rest are picked up by the next --resume."""
    _seed_securities(conn, ["000660", "005930", "035720"])
    collector = _FakeDart(calls_per_symbol=2)
    orch = _orchestrator(conn, collector, monkeypatch)
    # 5 calls = two full symbols (2+2), then the third is refused.
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31",
                          max_api_calls=5)
    assert collector.visited == ["000660", "005930", "035720"]
    repo = Repository(conn)
    assert repo.scan_done_symbols(*SCOPE) == {"000660", "005930"}

    run = conn.execute(
        "SELECT status, error FROM collection_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert run["status"] == "quota"          # not 'error' — this was intentional
    assert "--resume" in run["error"]


def test_quota_is_not_recorded_as_a_failed_symbol(conn, monkeypatch):
    """The interrupted symbol must be absent from the cursor, not marked failed:
    'failed' would be a lie about the data, and it never got a chance to run."""
    _seed_securities(conn, ["000660", "005930"])
    collector = _FakeDart(calls_per_symbol=2)
    orch = _orchestrator(conn, collector, monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31",
                          max_api_calls=3)
    summary = Repository(conn).scan_summary(*SCOPE)
    assert summary == {"done": 1, "error": 0, "rows_written": 0, "api_calls": 2}


def test_stored_rcept_nos_are_handed_to_the_collector(conn, monkeypatch):
    """A repeat sweep must not re-open 원문 it has already parsed. DART filings
    are immutable once accepted, so a stored 접수번호 is final."""
    _seed_securities(conn, ["005930"])
    collector = _FakeDart(per_symbol={"005930": [_change("005930", "20260828001916")]})
    orch = _orchestrator(conn, collector, monkeypatch)
    orch.collect_coverage("KR", "stake", None, ["005930"], "2026-01-01", "2026-08-31")
    assert collector.seen_arg == [set()]        # nothing stored on the first pass

    orch.collect_coverage("KR", "stake", None, ["005930"], "2026-01-01", "2026-08-31")
    assert collector.seen_arg[-1] == {"20260828001916"}


def test_seen_rcept_nos_do_not_leak_between_symbols(conn, monkeypatch):
    """Each symbol gets its own company's 접수번호. Leaking one company's set to
    the next would skip another issuer's filings on an id collision."""
    _seed_securities(conn, ["000660", "005930"])
    collector = _FakeDart(per_symbol={"000660": [_change("000660", "RA")]})
    orch = _orchestrator(conn, collector, monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    collector.seen_arg.clear()
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    assert collector.seen_arg == [{"RA"}, set()]


def test_api_calls_are_attributed_per_symbol(conn, monkeypatch):
    """Per-symbol call counts are what a cost estimate for the full market is
    built from, so they must be a delta, not the running total."""
    _seed_securities(conn, ["000660", "005930"])
    collector = _FakeDart(calls_per_symbol=3)
    orch = _orchestrator(conn, collector, monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    rows = conn.execute(
        "SELECT symbol, api_calls FROM scan_progress ORDER BY symbol"
    ).fetchall()
    assert [(r["symbol"], r["api_calls"]) for r in rows] == [("000660", 3), ("005930", 3)]


def test_an_unimplemented_type_still_skips_before_touching_the_cursor(conn, monkeypatch):
    """A source that cannot serve the type is 'skipped', not a sweep of failures."""
    _seed_securities(conn, ["005930"])

    class _Bare(BaseCollector):
        source = "dart"

    orch = _orchestrator(conn, _Bare(), monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    run = conn.execute(
        "SELECT status FROM collection_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert run["status"] == "skipped"
    assert conn.execute("SELECT COUNT(*) AS n FROM scan_progress").fetchone()["n"] == 0


def test_not_supported_from_a_symbol_still_aborts_the_sweep(conn, monkeypatch):
    """NotSupportedError mid-loop is a source-level fact, not a per-symbol one —
    marking symbols failed one by one would burn the whole market on it."""
    _seed_securities(conn, ["000660", "005930"])

    class _Unsupported(_FakeDart):
        def fetch_stake_changes(self, symbol, start, end):
            raise NotSupportedError("no key")

    orch = _orchestrator(conn, _Unsupported(), monkeypatch)
    orch.collect_coverage("KR", "stake", None, None, "2026-01-01", "2026-08-31")
    assert conn.execute("SELECT COUNT(*) AS n FROM scan_progress").fetchone()["n"] == 0
