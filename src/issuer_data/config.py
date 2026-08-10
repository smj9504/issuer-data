"""Application configuration loaded from environment / .env (prefix ISSUER_)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. All API keys are optional; a missing key skips its source."""

    model_config = SettingsConfigDict(
        env_prefix="ISSUER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Secrets / API keys -------------------------------------------------
    dart_api_key: str | None = None
    fmp_api_key: str | None = None
    alphavantage_api_key: str | None = None

    # 국가법령정보 공동활용 OpenAPI (open.law.go.kr) — OC is the email-id issued on
    # signup (e.g. "abcd" for abcd@korea.kr), not a traditional API key.
    law_api_oc: str | None = None

    # SEC EDGAR requires a descriptive UA with contact info or it returns 403.
    sec_user_agent: str = "issuer-data research your-email@example.com"

    # --- Storage ------------------------------------------------------------
    db_path: Path = Path("data/issuer_data.sqlite")
    docs_dir: Path = Path("data/documents")
    overrides_path: Path = Path("data/company_overrides.csv")

    # --- HTTP / rate limiting ----------------------------------------------
    http_timeout: float = 30.0
    max_retries: int = 4
    edgar_rate_limit: float = 8.0        # req/sec (SEC hard cap is 10)
    fmp_rate_limit: float = 4.0          # req/sec
    alphavantage_rate_limit: float = 0.08  # ~5/min free tier -> ~1 per 12.5s
    default_rate_limit: float = 2.0      # req/sec for generic sources
    law_rate_limit: float = 1.0          # req/sec — no published cap; stay polite


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
