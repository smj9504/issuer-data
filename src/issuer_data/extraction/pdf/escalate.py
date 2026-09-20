"""Optional paid escalation for low-confidence PDF tables.

The local structured engine (`pdf_extract.py`) grounds every extracted number
against the raw text layer and yields a per-table confidence. Escalation takes
ONLY the tables below a confidence threshold and asks a stronger (paid) engine to
reconstruct them — never the whole corpus, keeping cost proportional to the
low-confidence tail.

Escalation is **off and local-only by default**. `build_escalator` returns a
`NullEscalator` (which never leaves the box) unless the user has explicitly set
`pdf_escalation_enabled=True`, cleared `pdf_local_only`, and provided a key — so
sensitive documents never egress by accident.

Two backends share one interface:
- ``TextReconstructionEscalator`` (implemented): sends the low-confidence page's
  raw text layer + the garbled rows to an LLM and parses clean JSON rows back. No
  image rendering, so it is language-agnostic and works on any digital PDF.
- ``VisionEscalator`` (extension point): identical interface, for scanned pages;
  needs a page-render dependency and is not wired yet.

An escalator returns ``None`` on any failure — it never fabricates a table.
"""

from __future__ import annotations

import json
import re
from typing import Protocol, runtime_checkable

from .config import Settings
from .http.client import HttpClient
from .logging import get_logger

log = get_logger(__name__)


@runtime_checkable
class Escalator(Protocol):
    name: str

    def escalate(self, rows: list[list[str]], context_text: str,
                 col_count: int) -> list[list[str]] | None:
        ...


class NullEscalator:
    """Default: never escalates, nothing leaves the machine."""

    name = "none"

    def escalate(self, rows, context_text, col_count):
        return None


def choose_escalations(tables, threshold: float) -> list[int]:
    """Indices of tables whose grounding confidence is below the threshold."""
    return [i for i, t in enumerate(tables) if (t.confidence or 0.0) < threshold]


def build_escalator(settings: Settings) -> Escalator:
    """Return a real escalator only when fully opted in; else the null one.

    Guard order matters: local_only being True hard-blocks egress regardless of
    the enable flag, and a missing key falls back to local.
    """
    if not settings.pdf_escalation_enabled:
        return NullEscalator()
    if settings.pdf_local_only:
        log.info("PDF escalation enabled but local_only=True → staying local (no egress)")
        return NullEscalator()
    if not settings.escalation_api_key:
        log.warning("PDF escalation enabled but no escalation_api_key → staying local")
        return NullEscalator()
    return TextReconstructionEscalator(settings)


_PROMPT = (
    "You are correcting a table extracted from a PDF page. The local extractor "
    "produced rows that may be garbled or incomplete. Using the page's raw text as "
    "ground truth, return the corrected table as STRICT JSON of the form "
    '{{"rows": [["cell", ...], ...]}} with exactly {cols} columns per row and no '
    "commentary. Do not invent numbers that are absent from the raw text.\n\n"
    "=== RAW PAGE TEXT ===\n{context}\n\n=== EXTRACTED ROWS (JSON) ===\n{rows}\n"
)


class TextReconstructionEscalator:
    """Reconstruct a table from the page's text layer via an LLM (Anthropic shape)."""

    name = "text-llm"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = HttpClient(rate_limit=settings.default_rate_limit)

    def escalate(self, rows, context_text, col_count):
        prompt = _PROMPT.format(cols=col_count, context=context_text[:20000],
                                rows=json.dumps(rows, ensure_ascii=False))
        try:
            resp = self.client.request(
                "POST", self.settings.escalation_endpoint,
                headers={
                    "x-api-key": self.settings.escalation_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.settings.escalation_model,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("escalation request failed: %s", exc)
            return None
        return _parse_rows(_message_text(body))


def _message_text(body: dict) -> str:
    """Pull the assistant text out of an Anthropic Messages API response."""
    parts = body.get("content") if isinstance(body, dict) else None
    if not isinstance(parts, list):
        return ""
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _parse_rows(text: str) -> list[list[str]] | None:
    """Parse {"rows": [[...]]} out of an LLM reply, tolerating code fences/prose."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    rows = obj.get("rows") if isinstance(obj, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    out: list[list[str]] = []
    for row in rows:
        if not isinstance(row, list):
            return None
        out.append([("" if c is None else str(c).strip()) for c in row])
    return out


class VisionEscalator:
    """Extension point for scanned pages — needs a page-render dependency."""

    name = "vision-llm"

    def __init__(self, settings: Settings) -> None:
        raise NotSupportedError(
            "VisionEscalator needs a page-render backend (PyMuPDF/pdf2image); "
            "not wired yet — use TextReconstructionEscalator for digital PDFs"
        )

    def escalate(self, rows, context_text, col_count):  # pragma: no cover
        return None


class NotSupportedError(NotImplementedError):
    """Raised when an escalation backend is not available."""
