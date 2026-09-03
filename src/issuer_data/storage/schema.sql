-- Canonical schema for the issuer-data SQLite database.
-- Two-tier entity model (companies + securities) to support cross-listing
-- (US ADRs, HK/US dual listings) and cross-market comparison.

PRAGMA foreign_keys = ON;

-- Real-world issuer (one per company, spans markets) --------------------------
CREATE TABLE IF NOT EXISTS companies (
    company_id INTEGER PRIMARY KEY,
    name       TEXT,                     -- English / romanized
    local_name TEXT,                     -- 삼성전자 · 騰訊控股
    country    TEXT,                      -- domicile 'KR','HK','CN','US'
    sector     TEXT,
    industry   TEXT,
    lei        TEXT,
    cik        TEXT,
    corp_code  TEXT,                      -- DART corp_code
    isin       TEXT,
    website    TEXT,
    source     TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Individual listing / tradable line ------------------------------------------
CREATE TABLE IF NOT EXISTS securities (
    security_id   INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    market        TEXT NOT NULL,         -- 'KR'|'HK'|'US'
    symbol        TEXT NOT NULL,         -- '005930','00700','BABA','TCEHY'
    exchange      TEXT,                  -- 'KOSPI','KOSDAQ','HKEX','NYSE','NASDAQ','OTC'
    security_type TEXT,                  -- 'COMMON','ADR','GDR','PREFERRED'
    currency      TEXT,                  -- 'KRW','HKD','USD'
    isin          TEXT,
    listing_date  TEXT,
    is_primary    INTEGER DEFAULT 0,     -- 1 = the company's primary/home listing
    source        TEXT NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (market, symbol)
);
CREATE INDEX IF NOT EXISTS idx_sec_company ON securities(company_id);

-- Identifier crosswalk for cross-source entity resolution ---------------------
CREATE TABLE IF NOT EXISTS identifier_xref (
    id_type    TEXT NOT NULL,            -- 'ISIN','LEI','CIK','CORP_CODE','TICKER','ADR_TICKER'
    id_value   TEXT NOT NULL,
    company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    source     TEXT,
    PRIMARY KEY (id_type, id_value)
);

-- OHLCV per LISTING (each security trades in its own currency) -----------------
CREATE TABLE IF NOT EXISTS prices (
    security_id INTEGER NOT NULL REFERENCES securities(security_id) ON DELETE CASCADE,
    trade_date  TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    adj_close   REAL,
    currency    TEXT,
    source      TEXT NOT NULL,
    PRIMARY KEY (security_id, trade_date, source)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(trade_date);

-- Financial statements per COMPANY (long/tidy across markets) -----------------
CREATE TABLE IF NOT EXISTS financials (
    company_id     INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    fiscal_year    INTEGER NOT NULL,
    fiscal_period  TEXT NOT NULL,        -- 'FY','Q1'..'Q4','H1'
    fs_scope       TEXT NOT NULL DEFAULT 'CFS',  -- 'CFS'(연결/consolidated) | 'OFS'(별도/separate)
    statement_type TEXT,                 -- 'IS','BS','CF' (nullable)
    account        TEXT NOT NULL,        -- normalized concept/tag
    account_local  TEXT,                 -- '매출액' when available
    value          REAL,
    currency       TEXT,
    unit           TEXT,
    period_end     TEXT,
    source         TEXT NOT NULL,
    PRIMARY KEY (company_id, fiscal_year, fiscal_period, fs_scope, statement_type, account, source)
);

-- Disclosures per COMPANY -----------------------------------------------------
CREATE TABLE IF NOT EXISTS filings (
    company_id  INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    filing_id   TEXT NOT NULL,           -- rcept_no | accessionNumber | doc id
    filed_date  TEXT,
    filing_type TEXT,
    title       TEXT,
    url         TEXT,                     -- primary/viewer URL
    doc_urls    TEXT,                     -- JSON array of document URLs (primary first)
    source      TEXT NOT NULL,
    PRIMARY KEY (company_id, filing_id, source)
);

CREATE TABLE IF NOT EXISTS filing_documents (   -- downloaded originals + extracted text
    company_id   INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    filing_id    TEXT NOT NULL,
    source       TEXT NOT NULL,
    doc_seq      INTEGER NOT NULL DEFAULT 0,  -- 0=primary, 1..=exhibits/attachments
    doc_url      TEXT,
    local_path   TEXT,
    doc_format   TEXT,                    -- 'pdf','html','xml','txt','zip'
    file_size    INTEGER,
    text_content TEXT,                    -- extracted body text (NULL if OCR-needed)
    text_chars   INTEGER,
    downloaded_at TEXT,
    PRIMARY KEY (company_id, filing_id, source, doc_seq),
    FOREIGN KEY (company_id, filing_id, source)
        REFERENCES filings(company_id, filing_id, source) ON DELETE CASCADE
);

-- Structured tables extracted from filing PDFs (cross-page-stitched). Long/tidy:
-- one row per cell. confidence = fraction of the table's numbers grounded in the
-- raw text layer; source_engine names the extractor for provenance.
CREATE TABLE IF NOT EXISTS filing_tables (
    company_id    INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    filing_id     TEXT NOT NULL,
    source        TEXT NOT NULL,
    doc_seq       INTEGER NOT NULL DEFAULT 0,
    table_seq     INTEGER NOT NULL,        -- 0..N tables within the document
    row_idx       INTEGER NOT NULL,
    col_idx       INTEGER NOT NULL,
    value         TEXT,
    page_start    INTEGER,
    page_end      INTEGER,                 -- > page_start when the table was stitched
    confidence    REAL,
    needs_review  INTEGER DEFAULT 0,       -- 1 = below the confidence threshold, not resolved
    source_engine TEXT,
    PRIMARY KEY (company_id, filing_id, source, doc_seq, table_seq, row_idx, col_idx)
);
CREATE INDEX IF NOT EXISTS idx_filing_tables_doc
    ON filing_tables(company_id, filing_id, source, doc_seq);

-- Daily FX for local <-> USD normalization ------------------------------------
-- rate_type 'spot' = daily close; 'avg' = mean of daily spot over a fiscal period
-- (rate_date = that period's period_end). Accounting-correct USD conversion uses
-- 'avg' for IS/CF flows and 'spot' for BS stocks / prices.
CREATE TABLE IF NOT EXISTS fx_rates (
    rate_date TEXT NOT NULL,
    base_ccy  TEXT NOT NULL,             -- 'KRW','HKD','USD'
    quote_ccy TEXT NOT NULL,             -- usually 'USD'
    rate_type TEXT NOT NULL DEFAULT 'spot',  -- 'spot' | 'avg'
    rate      REAL NOT NULL,             -- 1 base_ccy = rate quote_ccy
    source    TEXT NOT NULL,
    PRIMARY KEY (rate_date, base_ccy, quote_ccy, rate_type, source)
);
CREATE INDEX IF NOT EXISTS idx_fx_lookup ON fx_rates(base_ccy, quote_ccy, rate_type, rate_date);

-- Peer relationships for side-by-side comparison ------------------------------
CREATE TABLE IF NOT EXISTS company_peers (
    company_id      INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    peer_company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    relation        TEXT,                -- 'fmp_peer','same_sector','manual'
    source          TEXT NOT NULL,
    PRIMARY KEY (company_id, peer_company_id, source)
);

-- Collection bookkeeping ------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id       INTEGER PRIMARY KEY,
    market       TEXT,
    data_type    TEXT,
    source       TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,
    rows_written INTEGER,
    error        TEXT
);

-- Scan cursor: where a per-symbol sweep got to -------------------------------
-- A full-market DART sweep is thousands of symbols x several API calls each and
-- will hit the daily quota partway through. Restarting from the top would burn
-- the next day's quota re-fetching what is already stored, so each symbol's
-- outcome is recorded as it completes and `--resume` skips the done ones.
-- Keyed on the scope actually being swept (market/data_type/source + the date
-- range), because the same symbol finished for 2025 is NOT finished for 2026.
CREATE TABLE IF NOT EXISTS scan_progress (
    market      TEXT NOT NULL,
    data_type   TEXT NOT NULL,
    source      TEXT NOT NULL,
    range_start TEXT NOT NULL DEFAULT '',
    range_end   TEXT NOT NULL DEFAULT '',
    symbol      TEXT NOT NULL,
    status      TEXT NOT NULL,           -- 'done' | 'error'
    rows_written INTEGER DEFAULT 0,
    api_calls   INTEGER DEFAULT 0,
    error       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (market, data_type, source, range_start, range_end, symbol)
);
CREATE INDEX IF NOT EXISTS idx_scan_progress_status ON scan_progress(status);

-- Daily API-call ledger -------------------------------------------------------
-- OpenDART meters per calendar day per key (20,000/day at time of writing) and
-- locks the account out on overrun, so the budget is per-day state that has to
-- survive process restarts — an in-memory counter would reset on every re-run
-- and walk straight past the cap.
CREATE TABLE IF NOT EXISTS api_call_budget (
    source    TEXT NOT NULL,
    call_date TEXT NOT NULL,             -- YYYY-MM-DD, local calendar day
    calls     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, call_date)
);

-- Cross-market comparison views (local + USD in one place) --------------------
-- Latest price per security with USD conversion via the NEAREST-PRIOR fx_rate
-- (so holidays / small range gaps still resolve).
DROP VIEW IF EXISTS v_latest_price;
CREATE VIEW v_latest_price AS
SELECT s.company_id, s.security_id, s.market, s.symbol, s.security_type, s.currency,
       p.trade_date,
       p.close AS close_local,
       CASE WHEN p.currency = 'USD' THEN p.close
            ELSE p.close * (
                SELECT fx.rate FROM fx_rates fx
                WHERE fx.base_ccy = p.currency AND fx.quote_ccy = 'USD'
                  AND fx.rate_type = 'spot' AND fx.rate_date <= p.trade_date
                ORDER BY fx.rate_date DESC LIMIT 1
            )
       END AS close_usd
FROM securities s
JOIN prices p ON p.security_id = s.security_id
WHERE p.trade_date = (
    SELECT MAX(p2.trade_date) FROM prices p2 WHERE p2.security_id = s.security_id
);

-- One row per company: all listings concatenated, for multi-market display.
DROP VIEW IF EXISTS v_company_overview;
CREATE VIEW v_company_overview AS
SELECT c.company_id, c.name, c.local_name, c.country, c.sector, c.industry,
       c.cik, c.corp_code, c.isin,
       GROUP_CONCAT(s.market || ':' || s.symbol || '(' || COALESCE(s.security_type, '?') || ')') AS listings
FROM companies c
LEFT JOIN securities s ON s.company_id = c.company_id
GROUP BY c.company_id;

-- ============================================================================
-- Extension B: comprehensive coverage tables
-- ============================================================================

-- Per-listing daily metrics (market cap, shares, foreign ownership) -----------
CREATE TABLE IF NOT EXISTS daily_metrics (
    security_id        INTEGER NOT NULL REFERENCES securities(security_id) ON DELETE CASCADE,
    metric_date        TEXT NOT NULL,
    market_cap         REAL,
    shares_outstanding REAL,
    foreign_own_pct    REAL,             -- KRX only (login) — usually NULL
    currency           TEXT,
    source             TEXT NOT NULL,
    PRIMARY KEY (security_id, metric_date, source)
);

-- Financial ratios / valuation per company/period -----------------------------
CREATE TABLE IF NOT EXISTS ratios (
    company_id    INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    fiscal_year   INTEGER NOT NULL,
    fiscal_period TEXT NOT NULL DEFAULT 'FY',
    metric        TEXT NOT NULL,         -- 'PER','PBR','EV/EBITDA','ROE','debtRatio',...
    value         REAL,
    source        TEXT NOT NULL,
    PRIMARY KEY (company_id, fiscal_year, fiscal_period, metric, source)
);

-- Ownership structure (major holders, related parties, treasury) ---------------
CREATE TABLE IF NOT EXISTS ownership (
    company_id  INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    as_of_date  TEXT NOT NULL,
    holder_name TEXT NOT NULL,
    holder_type TEXT,                    -- 'major','related','institution','foreign','treasury','5pct'
    shares      REAL,
    pct         REAL,
    source      TEXT NOT NULL,
    PRIMARY KEY (company_id, as_of_date, holder_name, holder_type, source)
);

-- KR 지분 변동 명세: one 변동 row of a 주식등의대량보유상황보고서 (DART D001) ----
-- Stores DART's own codes, not a derived verdict. The filer declares both the
-- relationship (relation: 최대주주/계열회사등/기타) and the counterparty type
-- (holder_type: 금융기관/국내법인/개인), so "is this an FI or the 대주주" is a
-- query-time definition — see v_kr_stake_sales. Baking it in here would mean
-- re-fetching every document whenever the definition changed, and would throw
-- away the evidence for the call.
-- No unit price: the 변동명세 has no price field in any document sampled.
CREATE TABLE IF NOT EXISTS kr_stake_changes (
    company_id        INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    rcept_no          TEXT NOT NULL,      -- DART 접수번호; the deal-level key
    change_date       TEXT NOT NULL,      -- MDF_DT
    holder_name       TEXT NOT NULL,      -- SPC_NM
    holder_id         TEXT NOT NULL DEFAULT '',  -- SPC_ID2 (사업자번호). '' not NULL:
                                          -- part of the PK, and NULL <> NULL would
                                          -- defeat dedup for every unidentified holder
    holder_type       TEXT,               -- SPC_TP code
    holder_type_label TEXT,
    relation          TEXT,               -- FLT_CRP_RLT code
    relation_label    TEXT,
    method            TEXT,               -- HLD_MTH code (01/02/11/12/...)
    method_label      TEXT,               -- e.g. '장내매도(-)'
    stock_kind        TEXT NOT NULL DEFAULT '',  -- STK_KND code. '' not NULL: it is
                                          -- part of the PK, and NULL <> NULL would
                                          -- defeat dedup exactly as it would for holder_id
    shares_before     REAL,
    shares_delta      REAL,               -- signed
    report_type       TEXT,               -- RPT_DST1: 신규/변동/변경
    source            TEXT NOT NULL,
    -- stock_kind belongs in the key: one holder can move 의결권있는 주식 (11) and
    -- 기타 (10, 우선주/신주인수권 etc.) on the same date by the same method, as two
    -- separate 변동 lines. Without it the second silently overwrites the first and
    -- the quantity vanishes — 4 of 64 rows on 20260805000440 did exactly that.
    PRIMARY KEY (company_id, rcept_no, change_date, holder_name, holder_id, method,
                 stock_kind, source)
);
CREATE INDEX IF NOT EXISTS idx_kr_stake_changes_date ON kr_stake_changes(change_date);
CREATE INDEX IF NOT EXISTS idx_kr_stake_changes_holder ON kr_stake_changes(holder_id);

-- KR 자기주식 취득/처분 (DART 주요사항보고 B) ----------------------------------
-- Unlike a stake change this carries an execution price, which is what makes
-- price-consistency checks against `prices` possible.
CREATE TABLE IF NOT EXISTS kr_treasury_disposals (
    company_id   INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    rcept_no     TEXT NOT NULL,
    report_date  TEXT NOT NULL,
    report_kind  TEXT NOT NULL,           -- 처분결정 | 취득결정 | 결과보고서
    shares       REAL,                    -- SEL_OSTK
    unit_price   REAL,                    -- SEL_OSTK_SPRC — actual execution price
    total_amount REAL,                    -- SEL_OSTK_PRC
    counterparty TEXT,                    -- DSPS_PARN — 처분 상대방 (the buyer)
    purpose      TEXT,                    -- SEL_PPS
    method       TEXT,
    source       TEXT NOT NULL,
    PRIMARY KEY (company_id, rcept_no, report_kind, source)
);
CREATE INDEX IF NOT EXISTS idx_kr_treasury_date ON kr_treasury_disposals(report_date);

-- Institutional holdings (US 13F; KR 국민연금 via 5% reports) ------------------
CREATE TABLE IF NOT EXISTS institutional_holdings (
    company_id   INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    quarter      TEXT NOT NULL,          -- 'YYYY-Qn' or period-end date
    manager      TEXT NOT NULL,          -- institution / filer name
    shares       REAL,
    value        REAL,
    source       TEXT NOT NULL,
    PRIMARY KEY (company_id, quarter, manager, source)
);

-- Corporate actions (dividends, splits, rights, mergers, buybacks) ------------
CREATE TABLE IF NOT EXISTS corporate_actions (
    security_id INTEGER NOT NULL REFERENCES securities(security_id) ON DELETE CASCADE,
    ex_date     TEXT NOT NULL,
    action_type TEXT NOT NULL,           -- 'dividend','split','rights','merger','buyback'
    ratio       REAL,                    -- split ratio
    amount      REAL,                    -- dividend amount / cash
    currency    TEXT,
    source      TEXT NOT NULL,
    PRIMARY KEY (security_id, ex_date, action_type, source)
);

-- Analyst estimates / targets / recommendations -------------------------------
CREATE TABLE IF NOT EXISTS analyst_estimates (
    company_id  INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    fiscal_year INTEGER NOT NULL,
    metric      TEXT NOT NULL,           -- 'revenue','eps','ebitda',...
    avg_est REAL, high_est REAL, low_est REAL, num_analysts INTEGER,
    source      TEXT NOT NULL,
    PRIMARY KEY (company_id, fiscal_year, metric, source)
);
CREATE TABLE IF NOT EXISTS price_targets (
    company_id  INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    target_date TEXT NOT NULL,
    target      REAL, analyst TEXT,
    source      TEXT NOT NULL,
    PRIMARY KEY (company_id, target_date, analyst, source)
);
CREATE TABLE IF NOT EXISTS recommendations (
    company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    rec_date   TEXT NOT NULL,
    grade      TEXT,                     -- consensus or grade text
    strong_buy INTEGER, buy INTEGER, hold INTEGER, sell INTEGER, strong_sell INTEGER,
    source     TEXT NOT NULL,
    PRIMARY KEY (company_id, rec_date, source)
);

-- Insider / officer transactions (US Form 4; KR 임원·주요주주) -----------------
CREATE TABLE IF NOT EXISTS insider_trades (
    company_id  INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    filed_date  TEXT NOT NULL,
    insider     TEXT NOT NULL,
    relation    TEXT,                    -- officer/director/10% owner
    txn_type    TEXT NOT NULL DEFAULT 'hold',  -- 'buy'/'sell'/'hold' (non-NULL for a stable PK)
    txn_seq     INTEGER NOT NULL DEFAULT 0,    -- distinguishes multiple txns in one filing
    shares      REAL, price REAL,
    filing_id   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL,
    PRIMARY KEY (company_id, filed_date, insider, txn_type, txn_seq, filing_id, source)
);

-- Earnings events (calendar; transcripts stored via filing_documents) ---------
CREATE TABLE IF NOT EXISTS earnings_events (
    company_id    INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    event_date    TEXT NOT NULL,
    event_type    TEXT,                  -- 'report','call'
    fiscal_period TEXT,
    eps_estimate REAL, eps_actual REAL,
    source        TEXT NOT NULL,
    PRIMARY KEY (company_id, event_date, event_type, source)
);

-- News + sentiment ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news (
    company_id   INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    published_at TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT,
    sentiment    REAL,
    source       TEXT NOT NULL,
    PRIMARY KEY (company_id, published_at, title, source)
);

-- Demand/oversubscription signals for an offering (US has no order-book
-- disclosure requirement, so this is best-effort from free official sources:
-- Nasdaq's IPO calendar deal-size changes, SEC EDGAR full-text search hits
-- ('oversubscribed' / 'testing-the-waters'), IPO price-band escalation,
-- anchor-investor "indication of interest" disclosures, and the length of
-- the confidential DRS review period) --
CREATE TABLE IF NOT EXISTS demand_signals (
    company_id      INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    signal_date     TEXT NOT NULL,
    -- 'nasdaq_calendar' | 'sec_fulltext' | 'price_band' | 'anchor_investor'
    -- | 'ttw_fulltext' | 'confidential_review'
    signal_type     TEXT NOT NULL,
    filed_amount    REAL,                 -- as-proposed deal size (USD)
    priced_amount   REAL,                 -- final confirmed deal size (USD)
    price           REAL,
    shares          REAL,
    -- anchor-investor name (best-effort text extraction); '' for every other
    -- signal_type. Part of the primary key, so it must stay NOT NULL — a
    -- nullable column here would break dedup for all other signal types
    -- (SQL treats NULL <> NULL, so two NULL rows never collide).
    investor_name   TEXT NOT NULL DEFAULT '',
    indicated_amount REAL,                -- anchor investor's indicated $ (USD)
    detail          TEXT,                 -- quoted snippet / summary
    url             TEXT,
    source          TEXT NOT NULL,
    PRIMARY KEY (company_id, signal_date, signal_type, source, investor_name)
);

-- Index membership history (FMP US indices) -----------------------------------
CREATE TABLE IF NOT EXISTS index_membership (
    company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    index_name TEXT NOT NULL,            -- 'S&P500','NASDAQ100','DOWJONES'
    added      TEXT,
    removed    TEXT,
    source     TEXT NOT NULL,
    PRIMARY KEY (company_id, index_name, source)
);

-- ESG scores ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS esg_scores (
    company_id INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    period     TEXT NOT NULL,
    env REAL, soc REAL, gov REAL, total REAL,
    source     TEXT NOT NULL,
    PRIMARY KEY (company_id, period, source)
);

-- ============================================================================
-- Korean statutes (국가법령정보 공동활용 OpenAPI, open.law.go.kr)
-- National reference data, not market/symbol-scoped. company_id is an optional
-- link a caller may attach (e.g. "이 법령은 이 발행사와 관련 있음") — nullable.
-- ============================================================================
CREATE TABLE IF NOT EXISTS statutes (
    law_id            TEXT NOT NULL,        -- 법령ID
    law_serial_no     TEXT NOT NULL,        -- 법령일련번호 (MST)
    name              TEXT NOT NULL,        -- 법령명한글
    name_abbrev       TEXT,
    law_type          TEXT,                 -- 법률/시행령/시행규칙/...
    department        TEXT,                 -- 소관부처명
    promulgation_date TEXT,
    promulgation_no    TEXT,
    enforcement_date  TEXT,
    revision_type     TEXT,                 -- 제개정구분명
    detail_url        TEXT,
    articles_json     TEXT,                 -- JSON list of {article_no,title,content}; body fetch only
    raw_text          TEXT,                 -- raw API payload fallback (body fetch only)
    company_id        INTEGER REFERENCES companies(company_id) ON DELETE SET NULL,
    source            TEXT NOT NULL,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (law_id, source)
);
CREATE INDEX IF NOT EXISTS idx_statutes_company ON statutes(company_id);

CREATE TABLE IF NOT EXISTS statute_history (
    law_id            TEXT NOT NULL,
    law_serial_no     TEXT NOT NULL,        -- MST of this specific revision
    name              TEXT NOT NULL,
    enforcement_date  TEXT,
    promulgation_date TEXT,
    revision_type     TEXT,
    status            TEXT,                 -- 현행연혁코드: '현행'|'연혁'|'시행예정'
    company_id        INTEGER REFERENCES companies(company_id) ON DELETE SET NULL,
    source            TEXT NOT NULL,
    PRIMARY KEY (law_id, law_serial_no, source)
);

CREATE TABLE IF NOT EXISTS statute_translations (
    law_id        TEXT NOT NULL,
    law_serial_no TEXT,
    name_en       TEXT NOT NULL,
    content_en    TEXT,
    company_id    INTEGER REFERENCES companies(company_id) ON DELETE SET NULL,
    source        TEXT NOT NULL,
    PRIMARY KEY (law_id, source)
);

CREATE TABLE IF NOT EXISTS statute_comparisons (
    law_id     TEXT NOT NULL,
    article_no TEXT NOT NULL,
    old_text   TEXT,
    new_text   TEXT,
    company_id INTEGER REFERENCES companies(company_id) ON DELETE SET NULL,
    source     TEXT NOT NULL,
    PRIMARY KEY (law_id, article_no, source)
);

-- Catch-all for every other 국가법령정보 OpenAPI category (행정규칙/자치법규/판례/
-- 법령해석례/헌재결정례/조약/별표서식/법령체계도/법령명약칭/...) — raw JSON per item,
-- since those targets aren't individually modeled (see LawApiRawItem).
CREATE TABLE IF NOT EXISTS law_api_raw (
    target       TEXT NOT NULL,       -- API target code, e.g. 'admrul','prec','ordin'
    item_key     TEXT NOT NULL,       -- best-effort id within target; content hash fallback
    title        TEXT,
    payload_json TEXT NOT NULL,
    company_id   INTEGER REFERENCES companies(company_id) ON DELETE SET NULL,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (target, item_key, source)
);

-- Accounting-correct USD-converted financials --------------------------------
-- IS/CF (flows) -> period-average rate; BS (stocks) -> nearest-prior spot at period_end.
DROP VIEW IF EXISTS v_financials_usd;
CREATE VIEW v_financials_usd AS
SELECT f.company_id, f.fiscal_year, f.fiscal_period, f.fs_scope, f.statement_type,
       f.account, f.account_local, f.value AS value_local, f.currency, f.period_end, f.source,
       CASE
         WHEN f.currency = 'USD' THEN f.value
         WHEN f.statement_type IN ('IS','CF') THEN f.value * (
            SELECT fx.rate FROM fx_rates fx
            WHERE fx.base_ccy = f.currency AND fx.quote_ccy = 'USD' AND fx.rate_type = 'avg'
              AND fx.rate_date <= COALESCE(f.period_end, printf('%04d-12-31', f.fiscal_year))
            ORDER BY fx.rate_date DESC LIMIT 1)
         ELSE f.value * (
            SELECT fx.rate FROM fx_rates fx
            WHERE fx.base_ccy = f.currency AND fx.quote_ccy = 'USD' AND fx.rate_type = 'spot'
              AND fx.rate_date <= COALESCE(f.period_end, printf('%04d-12-31', f.fiscal_year))
            ORDER BY fx.rate_date DESC LIMIT 1)
       END AS value_usd
FROM financials f;

-- KR 지분 매각 분류 / 딜 집계 / 가격 정합성 ------------------------------------
-- The classification layer. kr_stake_changes stores only what DART declared, so
-- "매도인가", "FI인가", "딜이 무엇인가" are all defined *here* and can be redefined
-- by editing this view alone — no re-fetching, and several definitions can coexist.

-- One row per 변동 line, with side and holder bucket derived from DART's codes.
DROP VIEW IF EXISTS v_kr_stake_sales;
CREATE VIEW v_kr_stake_sales AS
SELECT
    s.company_id, s.rcept_no, s.change_date, s.holder_name, s.holder_id,
    s.method, s.method_label, s.relation, s.relation_label,
    s.holder_type, s.holder_type_label, s.shares_before, s.shares_delta,
    -- Side: the sign is carried in HLD_MTH's label suffix, and the code itself is
    -- authoritative for the venue. shares_delta alone would misclassify the rows
    -- where a filer reports an unsigned quantity.
    CASE
        WHEN s.method IN ('02','12') THEN 'sell'
        WHEN s.method IN ('01','11') THEN 'buy'
        WHEN s.method_label LIKE '%(-)%' THEN 'sell'
        WHEN s.method_label LIKE '%(+)%' THEN 'buy'
        WHEN s.shares_delta < 0 THEN 'sell'
        WHEN s.shares_delta > 0 THEN 'buy'
        ELSE 'other'
    END AS side,
    -- Venue: 장내 vs 장외 matters because a block deal is an off-market trade.
    CASE
        WHEN s.method IN ('01','02') THEN 'on_market'
        WHEN s.method IN ('11','12') THEN 'off_market'
        ELSE 'other'
    END AS venue,
    -- Holder bucket. This is the "FI vs 대주주" call, and it is deliberately a
    -- view definition rather than stored data: FLT_CRP_RLT is the issuer's own
    -- declaration of the relationship, SPC_TP of the entity type. A 금융기관 that
    -- is NOT 최대주주/계열회사 is the closest DART gets to declaring a financial
    -- investor. Redefine here if a study needs a stricter or looser rule.
    CASE
        WHEN s.relation IN ('10') THEN 'controlling'      -- 최대주주
        WHEN s.relation IN ('14') THEN 'affiliate'        -- 계열회사등
        WHEN s.holder_type = 'K' THEN 'financial_investor'-- 금융기관, unaffiliated
        WHEN s.holder_type = 'D' THEN 'individual'
        ELSE 'other'
    END AS holder_bucket
FROM kr_stake_changes s;

-- Deal-level rollup. A block deal shows up as several 특별관계자 selling on the
-- same date under one 접수번호, so the deal grain is (rcept_no, change_date, side),
-- not the individual holder line.
DROP VIEW IF EXISTS v_kr_stake_deals;
CREATE VIEW v_kr_stake_deals AS
SELECT
    v.company_id, v.rcept_no, v.change_date, v.side, v.venue,
    COUNT(*)                       AS holder_lines,
    COUNT(DISTINCT v.holder_id)    AS holders,
    SUM(ABS(v.shares_delta))       AS shares_total,
    GROUP_CONCAT(DISTINCT v.holder_bucket) AS buckets,
    GROUP_CONCAT(DISTINCT v.holder_name)   AS holder_names
FROM v_kr_stake_sales v
WHERE v.side IN ('sell','buy')
GROUP BY v.company_id, v.rcept_no, v.change_date, v.side, v.venue;

-- Price consistency. The 변동명세 carries no unit price, so a stake change is
-- priced against that day's close (nearest prior, for a non-trading change_date);
-- 자기주식처분 carries its own execution price and is checked against the market
-- directly, which is what surfaces a discount typical of a negotiated placement.
DROP VIEW IF EXISTS v_kr_treasury_price_check;
CREATE VIEW v_kr_treasury_price_check AS
SELECT
    t.company_id, t.rcept_no, t.report_date, t.report_kind,
    t.shares, t.unit_price, t.total_amount, t.counterparty, t.purpose,
    mkt.close AS market_close,
    mkt.trade_date AS market_date,
    CASE WHEN mkt.close > 0 AND t.unit_price IS NOT NULL
         THEN ROUND((t.unit_price - mkt.close) / mkt.close * 100.0, 2)
    END AS premium_pct,
    -- shares * unit_price should reconcile to the reported total; a mismatch means
    -- a partially-filled or restated report rather than a parsing error.
    CASE WHEN t.shares IS NOT NULL AND t.unit_price IS NOT NULL AND t.total_amount IS NOT NULL
         THEN ROUND(t.shares * t.unit_price - t.total_amount, 0)
    END AS amount_residual
FROM kr_treasury_disposals t
-- One close per (company, date): the same day is often present from several
-- sources (krx and yfinance both), and joining them all would emit a duplicate
-- disposal row per source and silently inflate any count over this view.
LEFT JOIN (
    SELECT s.company_id, p.trade_date, MIN(p.close) AS close
    FROM prices p JOIN securities s ON s.security_id = p.security_id
    WHERE s.market = 'KR'
    GROUP BY s.company_id, p.trade_date
) mkt
  ON mkt.company_id = t.company_id
 AND mkt.trade_date = (
     SELECT MAX(p2.trade_date) FROM prices p2
     JOIN securities s2 ON s2.security_id = p2.security_id
     WHERE s2.company_id = t.company_id AND p2.trade_date <= t.report_date
 );
