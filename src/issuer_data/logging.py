"""Logging setup for the issuer-data package."""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def setup_logging(level: str | int | None = None) -> None:
    """Configure root logging once. Level from arg or ISSUER_LOG_LEVEL env."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    if level is None:
        level = os.environ.get("ISSUER_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # yfinance / urllib3 are noisy at INFO
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
