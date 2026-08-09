"""issuer-data: multi-market issuer/security data collector.

Collects issuer/security master data, OHLCV prices, financial statements, and
disclosures/filings from Korea (DART, KRX), Hong Kong (HKEXnews), and the US
(SEC EDGAR), plus cross-market sources (yfinance, FMP, Alpha Vantage), into a
unified SQLite database with cross-listing entity resolution.
"""

__version__ = "0.1.0"
