"""Base collector interface and shared errors."""

from __future__ import annotations

from abc import ABC

from ..models import Filing, FinancialFact, FxRate, Price, SecurityRecord


class NotSupportedError(NotImplementedError):
    """Raised when a collector cannot serve a requested data type."""


class QuotaExceededError(RuntimeError):
    """Raised when a source's daily API-call budget is used up.

    Distinct from a per-symbol failure: continuing the sweep would spend calls
    the account no longer has (OpenDART locks the key out on overrun), so the
    orchestrator stops the loop and leaves the scan cursor where it is, rather
    than logging a warning and marching through the remaining symbols.
    """


# OpenDART meters requests per calendar day per key and locks the key out for the
# rest of the day on overrun. The published free-tier ceiling is 20,000/day; the
# suggested budget sits under it so a sweep stops on our own terms — resumable,
# with the cursor intact — rather than on the provider's.
DEFAULT_DAILY_CALL_BUDGET = 18_000


class CallBudget:
    """A day's remaining API calls for one source, backed by the database.

    Two things have to be true at once for a market-wide sweep to be safe:
    the count must survive a process restart (a re-run must not get a fresh
    allowance the provider will not honour), and running out must stop the
    sweep rather than degrade it. So the tally lives in `api_call_budget` and
    exhaustion raises `QuotaExceededError`.

    Writes are batched: `spend()` charges an in-memory counter and flushes to
    the database every `flush_every` calls, since a commit per HTTP request
    would dominate the cost of the request itself. `flush()` is called at each
    symbol boundary, so a crash loses at most one symbol's worth of count —
    and over-counting slightly is the safe direction to be wrong in.
    """

    def __init__(self, repo, source: str, limit: int, flush_every: int = 25) -> None:
        self.repo = repo
        self.source = source
        self.limit = limit
        self.flush_every = max(1, flush_every)
        self.spent_before = repo.api_calls_today(source)
        self.pending = 0

    @property
    def used(self) -> int:
        return self.spent_before + self.pending

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def spend(self, n: int = 1) -> None:
        if self.used + n > self.limit:
            self.flush()
            raise QuotaExceededError(
                f"{self.source} daily API budget exhausted "
                f"({self.used}/{self.limit} calls used today); "
                "re-run with --resume tomorrow to continue where this stopped"
            )
        self.pending += n
        if self.pending >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if self.pending:
            self.spent_before = self.repo.record_api_calls(self.source, self.pending)
            self.pending = 0


class BaseCollector(ABC):
    """Common interface. Concrete collectors override what they support.

    Every method returns validated pydantic models (never raw DataFrames).
    A collector that cannot serve a data type raises ``NotSupportedError``;
    the orchestrator catches it and skips.
    """

    market: str = "multi"
    source: str = "base"

    # --- master (company + listing) ----------------------------------------
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        raise NotSupportedError(f"{self.source} does not provide master data")

    # --- prices (per listing) ----------------------------------------------
    def fetch_prices(self, symbol: str, start: str, end: str) -> list[Price]:
        raise NotSupportedError(f"{self.source} does not provide prices")

    # --- financials (per company) ------------------------------------------
    def fetch_financials(self, symbol: str, years: int | None = None) -> list[FinancialFact]:
        raise NotSupportedError(f"{self.source} does not provide financials")

    # --- filings (per company) ---------------------------------------------
    def fetch_filings(self, symbol: str, start: str, end: str) -> list[Filing]:
        raise NotSupportedError(f"{self.source} does not provide filings")

    # --- peers (optional) ---------------------------------------------------
    def fetch_peers(self, symbol: str) -> list[str]:
        raise NotSupportedError(f"{self.source} does not provide peers")

    # --- fx (optional) ------------------------------------------------------
    def fetch_fx(self, pairs: list[tuple[str, str]], start: str, end: str) -> list[FxRate]:
        raise NotSupportedError(f"{self.source} does not provide fx rates")

    # --- Extension B: comprehensive coverage (all optional) -----------------
    # Each returns a list of the matching pydantic model; default = NotSupported.
    def fetch_daily_metrics(self, symbol: str, start: str, end: str):
        raise NotSupportedError(f"{self.source} does not provide daily metrics")

    def fetch_ratios(self, symbol: str, years: int | None = None):
        raise NotSupportedError(f"{self.source} does not provide ratios")

    def fetch_ownership(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide ownership")

    def fetch_institutional(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide institutional holdings")

    def fetch_corporate_actions(self, symbol: str, start: str, end: str):
        raise NotSupportedError(f"{self.source} does not provide corporate actions")

    def fetch_analyst(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide analyst estimates")

    def fetch_insiders(self, symbol: str, start: str, end: str):
        raise NotSupportedError(f"{self.source} does not provide insider trades")

    def fetch_earnings(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide earnings events")

    def fetch_news(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide news")

    def fetch_index_membership(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide index membership")

    def fetch_esg(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide ESG")

    def fetch_demand_signals(self, symbol: str):
        raise NotSupportedError(f"{self.source} does not provide demand signals")

    def fetch_stake_changes(self, symbol: str, start: str, end: str):
        raise NotSupportedError(f"{self.source} does not provide stake-change detail")

    def fetch_treasury_disposals(self, symbol: str, start: str, end: str):
        raise NotSupportedError(f"{self.source} does not provide treasury disposals")
