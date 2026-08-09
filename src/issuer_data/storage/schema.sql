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
    statement_type TEXT,                 -- 'IS','BS','CF' (nullable in v1)
    account        TEXT NOT NULL,        -- normalized concept/tag
    account_local  TEXT,                 -- '매출액' when available
    value          REAL,
    currency       TEXT,
    unit           TEXT,
    period_end     TEXT,
    source         TEXT NOT NULL,
    PRIMARY KEY (company_id, fiscal_year, fiscal_period, statement_type, account, source)
);

-- Disclosures per COMPANY -----------------------------------------------------
CREATE TABLE IF NOT EXISTS filings (
    company_id  INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    filing_id   TEXT NOT NULL,           -- rcept_no | accessionNumber | doc id
    filed_date  TEXT,
    filing_type TEXT,
    title       TEXT,
    url         TEXT,
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

-- Daily FX for local <-> USD normalization ------------------------------------
CREATE TABLE IF NOT EXISTS fx_rates (
    rate_date TEXT NOT NULL,
    base_ccy  TEXT NOT NULL,             -- 'KRW','HKD','USD'
    quote_ccy TEXT NOT NULL,             -- usually 'USD'
    rate      REAL NOT NULL,             -- 1 base_ccy = rate quote_ccy
    source    TEXT NOT NULL,
    PRIMARY KEY (rate_date, base_ccy, quote_ccy, source)
);
CREATE INDEX IF NOT EXISTS idx_fx_lookup ON fx_rates(base_ccy, quote_ccy, rate_date);

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
-- Latest price per security with USD conversion via same-date fx_rate.
DROP VIEW IF EXISTS v_latest_price;
CREATE VIEW v_latest_price AS
SELECT s.company_id, s.security_id, s.market, s.symbol, s.security_type, s.currency,
       p.trade_date,
       p.close AS close_local,
       p.close * COALESCE(fx.rate, CASE WHEN p.currency = 'USD' THEN 1 END) AS close_usd
FROM securities s
JOIN prices p ON p.security_id = s.security_id
LEFT JOIN fx_rates fx
       ON fx.base_ccy = p.currency AND fx.quote_ccy = 'USD' AND fx.rate_date = p.trade_date
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
