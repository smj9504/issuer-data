"""Base collector interface and shared errors."""

from __future__ import annotations

from abc import ABC

from ..models import Filing, FinancialFact, FxRate, Price, SecurityRecord


class NotSupportedError(NotImplementedError):
    """Raised when a collector cannot serve a requested data type."""


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
