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
