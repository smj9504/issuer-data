"""Pydantic domain models. Collectors return these; storage persists them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from .utils.dates import to_iso


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


class Company(_Base):
    """Real-world issuer (spans markets)."""

    name: str | None = None
    local_name: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    lei: str | None = None
    cik: str | None = None
    corp_code: str | None = None
    isin: str | None = None
    website: str | None = None
    source: str


class Security(_Base):
    """An individual listing / tradable line."""

    market: str
    symbol: str
    exchange: str | None = None
    security_type: str | None = "COMMON"
    currency: str | None = None
    isin: str | None = None
    listing_date: str | None = None
    is_primary: bool = False
    source: str


class SecurityRecord(_Base):
    """Combined master row from a collector: company-level + listing-level fields.

    The resolver splits this into a Company + Security and assigns a company_id.
    """

    # listing-level (required)
    market: str
    symbol: str
    source: str
    # listing-level (optional)
    exchange: str | None = None
    security_type: str | None = "COMMON"
    currency: str | None = None
    listing_date: str | None = None
    is_primary: bool = False
    # company-level (optional identifiers used for cross-listing resolution)
    name: str | None = None
    local_name: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    lei: str | None = None
    cik: str | None = None
    corp_code: str | None = None
    isin: str | None = None
    website: str | None = None
    # optional pointer to an underlying (home) listing for ADR/GDR linkage
    underlying_isin: str | None = None
    underlying_symbol: str | None = None

    def to_company(self) -> Company:
        return Company(
            name=self.name,
            local_name=self.local_name,
            country=self.country,
            sector=self.sector,
            industry=self.industry,
            lei=self.lei,
            cik=self.cik,
            corp_code=self.corp_code,
            isin=self.isin,
            website=self.website,
            source=self.source,
        )

    def to_security(self) -> Security:
        return Security(
            market=self.market,
            symbol=self.symbol,
            exchange=self.exchange,
            security_type=self.security_type,
            currency=self.currency,
            isin=self.isin,
            listing_date=self.listing_date,
            is_primary=self.is_primary,
            source=self.source,
        )


class Price(_Base):
    """OHLCV row for one listing on one date."""

    symbol: str          # market symbol (resolved to security_id at storage)
    market: str
    trade_date: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    adj_close: float | None = None
    currency: str | None = None
    source: str

    @field_validator("trade_date", mode="before")
    @classmethod
    def _norm_date(cls, v):
        return to_iso(v)


class FinancialFact(_Base):
    """One financial statement data point (long/tidy)."""

    symbol: str          # resolved to company_id at storage
    market: str
    fiscal_year: int
    fiscal_period: str = "FY"
    fs_scope: str = "CFS"  # 'CFS'(연결/consolidated) | 'OFS'(별도/separate)
    statement_type: str | None = None
    account: str
    account_local: str | None = None
    value: float | None = None
    currency: str | None = None
    unit: str | None = None
    period_end: str | None = None
    source: str

    @field_validator("period_end", mode="before")
    @classmethod
    def _norm_pe(cls, v):
        return to_iso(v)


class Filing(_Base):
    """A disclosure/filing metadata row."""

    symbol: str          # resolved to company_id at storage
    market: str
    filing_id: str
    filed_date: str | None = None
    filing_type: str | None = None
    title: str | None = None
    url: str | None = None
    source: str
    # optional list of document URLs to download (primary first)
    doc_urls: list[str] = []

    @field_validator("filed_date", mode="before")
    @classmethod
    def _norm_fd(cls, v):
        return to_iso(v)


class FilingDocument(_Base):
    """A downloaded original document + extracted text."""

    company_id: int
    filing_id: str
    source: str
    doc_seq: int = 0
    doc_url: str | None = None
    local_path: str | None = None
    doc_format: str | None = None
    file_size: int | None = None
    text_content: str | None = None
    text_chars: int | None = None
    downloaded_at: str | None = None


class FxRate(_Base):
    """An FX rate: 1 base_ccy = rate quote_ccy. rate_type 'spot' (daily) or 'avg' (period)."""

    rate_date: str
    base_ccy: str
    quote_ccy: str = "USD"
    rate_type: str = "spot"
    rate: float
    source: str

    @field_validator("rate_date", mode="before")
    @classmethod
    def _norm_rd(cls, v):
        return to_iso(v)


class Peer(_Base):
    """A peer relationship (symbol-level; resolved to company ids at storage)."""

    symbol: str
    market: str
    peer_symbol: str
    peer_market: str
    relation: str = "fmp_peer"
    source: str


# --- Extension B coverage models (symbol-keyed; resolved at storage) ----------
class _SymBase(_Base):
    symbol: str
    market: str
    source: str


class DailyMetric(_SymBase):
    metric_date: str
    market_cap: float | None = None
    shares_outstanding: float | None = None
    foreign_own_pct: float | None = None
    currency: str | None = None


class Ratio(_SymBase):
    fiscal_year: int
    fiscal_period: str = "FY"
    metric: str
    value: float | None = None


class OwnershipRow(_SymBase):
    as_of_date: str
    holder_name: str
    holder_type: str | None = None
    shares: float | None = None
    pct: float | None = None


class InstitutionalHolding(_SymBase):
    quarter: str
    manager: str
    shares: float | None = None
    value: float | None = None


class CorporateAction(_SymBase):
    ex_date: str
    action_type: str
    ratio: float | None = None
    amount: float | None = None
    currency: str | None = None


class AnalystEstimate(_SymBase):
    fiscal_year: int
    metric: str
    avg_est: float | None = None
    high_est: float | None = None
    low_est: float | None = None
    num_analysts: int | None = None


class PriceTarget(_SymBase):
    target_date: str
    target: float | None = None
    analyst: str | None = None


class Recommendation(_SymBase):
    rec_date: str
    grade: str | None = None
    strong_buy: int | None = None
    buy: int | None = None
    hold: int | None = None
    sell: int | None = None
    strong_sell: int | None = None


class InsiderTrade(_SymBase):
    filed_date: str
    insider: str
    relation: str | None = None
    txn_type: str = "hold"          # 'buy'/'sell'/'hold' — non-NULL for a stable PK
    txn_seq: int = 0               # distinguishes multiple txns within one filing
    shares: float | None = None
    price: float | None = None
    filing_id: str = ""


class EarningsEvent(_SymBase):
    event_date: str
    event_type: str | None = None
    fiscal_period: str | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None


class NewsItem(_SymBase):
    published_at: str
    title: str
    url: str | None = None
    sentiment: float | None = None


class IndexMembership(_SymBase):
    index_name: str
    added: str | None = None
    removed: str | None = None


class EsgScore(_SymBase):
    period: str
    env: float | None = None
    soc: float | None = None
    gov: float | None = None
    total: float | None = None
