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


class StakeChange(_SymBase):
    """One 변동 line from a 주식등의대량보유상황보고서 (DART 지분공시 D001).

    Deliberately stores DART's own codes rather than a derived judgement. The
    filer already declares the relationship (`FLT_CRP_RLT`: 최대주주 / 계열회사등
    / 기타) and the counterparty type (`SPC_TP`: 금융기관 / 국내법인 / 개인), so
    classifying a holder as "FI" or "대주주" is a query-time definition, not a
    parse-time one: bake it in here and changing the definition means re-fetching
    every document, and the evidence for the call is gone.

    `method` is HLD_MTH (01=장내매수, 02=장내매도, 11=장외매수, 12=장외매도, ...);
    unrecognised codes are kept verbatim rather than dropped, since the observed
    dictionary is empirical and DART can add to it.

    There is no unit-price field in the 변동명세 — confirmed absent across every
    document sampled — so a stake change carries quantities only. Execution
    prices come from the 자기주식처분 side, or from `prices` on the change date.
    """

    rcept_no: str                        # DART 접수번호 (deal-level key)
    change_date: str                     # MDF_DT
    holder_name: str                     # SPC_NM — 보고자/특별관계자
    holder_id: str | None = None         # SPC_ID2 — 사업자등록번호/생년월일
    holder_type: str | None = None       # SPC_TP code (K/I/D/...)
    holder_type_label: str | None = None
    relation: str | None = None          # FLT_CRP_RLT code (10/14/16/...)
    relation_label: str | None = None
    method: str | None = None            # HLD_MTH code
    method_label: str | None = None      # e.g. '장내매도(-)'
    stock_kind: str | None = None        # STK_KND code
    shares_before: float | None = None   # BFR_MDF_CNT
    shares_delta: float | None = None    # MDF_SDK_CNT (signed)
    report_type: str | None = None       # RPT_DST1: 신규/변동/변경


class TreasuryDisposal(_SymBase):
    """자기주식 취득/처분 결정 or 결과 (DART 주요사항보고 B).

    Unlike a stake change this does carry an execution price (`unit_price`,
    SEL_OSTK_SPRC), which is what makes price-consistency checks possible at all.
    """

    rcept_no: str
    report_date: str
    report_kind: str                     # '처분결정' | '취득결정' | '결과보고서'
    shares: float | None = None          # SEL_OSTK
    unit_price: float | None = None      # SEL_OSTK_SPRC
    total_amount: float | None = None    # SEL_OSTK_PRC
    counterparty: str | None = None      # DSPS_PARN — 처분 상대방
    purpose: str | None = None           # SEL_PPS
    method: str | None = None


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


class DemandSignal(_SymBase):
    """A book-building / demand signal for an offering (IPO or follow-on).

    US filers have no obligation to disclose order-book detail (subscription
    ratio, order volume), unlike KR DART. This instead captures what free,
    official sources do reveal: deal-size changes on Nasdaq's IPO calendar
    (filed vs. priced amount, a proxy for "upsized due to demand") and
    self-reported "oversubscribed" language found in the company's own SEC
    filings via EDGAR full-text search.
    """

    signal_date: str
    # 'nasdaq_calendar' | 'sec_fulltext' | 'price_band' | 'anchor_investor'
    # | 'ttw_fulltext' | 'confidential_review'
    signal_type: str
    filed_amount: float | None = None
    priced_amount: float | None = None
    price: float | None = None
    shares: float | None = None
    # anchor-investor "indication of interest" signals only; "" (not None) for
    # every other signal_type — it's part of the table's primary key, and a
    # NULL column never collides with another NULL under SQL uniqueness rules,
    # which would silently break dedup for all the other signal types.
    investor_name: str = ""
    indicated_amount: float | None = None
    detail: str | None = None
    url: str | None = None

    @field_validator("signal_date", mode="before")
    @classmethod
    def _norm_sd(cls, v):
        return to_iso(v)


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


# --- Korean statutes (국가법령정보 공동활용 OpenAPI, open.law.go.kr) -----------
# Not symbol/market-scoped (national reference data); a caller may optionally
# link a fetched statute to one company_id at storage time (see Repository).
class StatuteArticle(_Base):
    """One 조(article) of a statute's body."""

    article_no: str          # '제1조' etc.
    article_title: str | None = None
    content: str


class StatuteRecord(_Base):
    """A 목록조회 (search) result row: statute metadata, no body text."""

    law_id: str               # 법령ID
    law_serial_no: str        # 법령일련번호 (MST) — required to fetch the body
    name: str                 # 법령명한글
    name_abbrev: str | None = None
    law_type: str | None = None        # 법령구분명 (법률/시행령/시행규칙/...)
    department: str | None = None      # 소관부처명
    promulgation_date: str | None = None
    promulgation_no: str | None = None
    enforcement_date: str | None = None
    revision_type: str | None = None   # 제개정구분명
    detail_url: str | None = None
    source: str = "law_go_kr"

    @field_validator("promulgation_date", "enforcement_date", mode="before")
    @classmethod
    def _norm_dates(cls, v):
        return to_iso(v)


class StatuteDetail(_Base):
    """A 본문조회 (body) result: full text, parsed into articles when possible."""

    law_id: str
    law_serial_no: str
    name: str
    department: str | None = None
    promulgation_date: str | None = None
    enforcement_date: str | None = None
    articles: list[StatuteArticle] = []
    raw_text: str | None = None   # full JSON dump as a fallback if parsing misses fields
    source: str = "law_go_kr"

    @field_validator("promulgation_date", "enforcement_date", mode="before")
    @classmethod
    def _norm_dates(cls, v):
        return to_iso(v)


class StatuteHistoryEntry(_Base):
    """One revision in a statute's 연혁 (amendment history)."""

    law_id: str
    law_serial_no: str
    name: str
    enforcement_date: str | None = None
    promulgation_date: str | None = None
    revision_type: str | None = None
    status: str | None = None  # 현행연혁코드: '현행'|'연혁'|'시행예정'
    source: str = "law_go_kr"

    @field_validator("promulgation_date", "enforcement_date", mode="before")
    @classmethod
    def _norm_dates(cls, v):
        return to_iso(v)


class StatuteTranslation(_Base):
    """영문법령 (official English translation) body."""

    law_id: str
    law_serial_no: str | None = None
    name_en: str
    content_en: str | None = None
    source: str = "law_go_kr"


class StatuteComparisonEntry(_Base):
    """One article's old-vs-new text from 신구법 or 3단비교."""

    law_id: str
    article_no: str
    old_text: str | None = None
    new_text: str | None = None
    source: str = "law_go_kr"


class LawApiRawItem(_Base):
    """One item from any other 국가법령정보 OpenAPI category (행정규칙/자치법규/판례/
    법령해석례/헌재결정례/조약/별표서식/법령체계도/법령명약칭/...). Kept as raw JSON —
    unlike StatuteRecord/Detail these targets' exact field shapes aren't individually
    modeled, so nothing is lost even for a category added after this code was written.
    """

    target: str          # API target code, e.g. 'admrul', 'prec', 'ordin'
    item_key: str         # best-effort id within target (falls back to a content hash)
    title: str | None = None
    payload_json: str
    source: str = "law_go_kr"
