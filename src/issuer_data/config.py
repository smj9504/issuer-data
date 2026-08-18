"""Application configuration loaded from environment / .env (prefix ISSUER_)."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
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

    # KRX data portal now requires a (free) member login for its JSON endpoints.
    # pykrx reads the UNPREFIXED KRX_ID / KRX_PW straight from os.environ, so these
    # read the same unprefixed names (ISSUER_-prefixed copies still work) instead of
    # silently staying None while pykrx is in fact authenticated.
    krx_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("KRX_ID", "ISSUER_KRX_ID"),
    )
    krx_pw: str | None = Field(
        default=None,
        validation_alias=AliasChoices("KRX_PW", "ISSUER_KRX_PW"),
    )

    # SEC EDGAR requires a descriptive UA with contact info or it returns 403.
    sec_user_agent: str = "issuer-data research your-email@example.com"

    # --- Storage ------------------------------------------------------------
    db_path: Path = Path("data/issuer_data.sqlite")
    docs_dir: Path = Path("data/documents")
    overrides_path: Path = Path("data/company_overrides.csv")

    # --- PDF structured extraction / escalation -----------------------------
    # The structured PDF engine runs locally and free. Escalation re-processes
    # ONLY low-confidence tables through a paid LLM — off, and local-only, by
    # default. local_only=True hard-blocks any off-box call even if enabled, so
    # sensitive documents never leave the machine unless explicitly opted in.
    pdf_escalation_enabled: bool = False
    pdf_local_only: bool = True
    pdf_confidence_threshold: float = 0.66   # tables below this are escalated / flagged
    escalation_api_key: str | None = None
    escalation_endpoint: str = "https://api.anthropic.com/v1/messages"
    escalation_model: str = "claude-sonnet-4-5"
    escalation_cost_per_page: float = 0.02   # for the cost-accounting log only

    # --- OCR (scanned PDFs) -------------------------------------------------
    # On by default: an image-only PDF is a document we would otherwise store with
    # no text at all, so a page with no text layer is rendered and OCR'd. The
    # Python side ships as a dependency; the Tesseract binary is a system package
    # (apt install tesseract-ocr tesseract-ocr-kor tesseract-ocr-chi-tra). When it
    # is missing, OCR is skipped with one warning saying exactly that.
    ocr_enabled: bool = True
    ocr_languages: str = "eng+kor+chi_tra"   # Tesseract lang codes, '+'-joined
    ocr_dpi: int = 300
    ocr_max_pages: int = 50                   # cap pages OCR'd per document

    # --- HTTP / rate limiting ----------------------------------------------
    http_timeout: float = 30.0
    max_retries: int = 4
    edgar_rate_limit: float = 8.0        # req/sec (SEC hard cap is 10)
    fmp_rate_limit: float = 4.0          # req/sec
    alphavantage_rate_limit: float = 0.08  # ~5/min free tier -> ~1 per 12.5s
    default_rate_limit: float = 2.0      # req/sec for generic sources


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
