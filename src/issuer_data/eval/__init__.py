"""PDF extraction accuracy evaluation: metrics, gold cases, and the harness."""

from .harness import default_cases, gate_calibration, run_eval, score_case
from .metrics import grits_con, numeric_exact_match, paragraph_continuity, teds

__all__ = [
    "default_cases",
    "gate_calibration",
    "grits_con",
    "numeric_exact_match",
    "paragraph_continuity",
    "run_eval",
    "score_case",
    "teds",
]
