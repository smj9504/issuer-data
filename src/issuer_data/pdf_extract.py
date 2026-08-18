"""Structured PDF extraction: cross-page table stitching + narrative reflow.

Template-, country-, and language-agnostic. The logic is geometry-based, not
keyed to any document form, so it works on Korean/US/HK annual reports and
government filings alike:

- **Tables** that break across a page boundary are stitched back together when
  the previous page's table ends at the bottom margin, the next page's table
  starts at the top margin, and their column x-signatures match; a repeated
  header row on the continuation is dropped.
- **Narrative** is reflowed into continuous text: running headers/footers and
  page-number lines (detected by recurrence across pages, not by pattern lists)
  are removed, line-end hyphenation is joined, and paragraphs split across lines
  or pages are merged. CJK text is joined without inserting spaces.
- **Numeric grounding** checks that every number emitted in a table also exists
  in the raw text layer, yielding a per-table confidence — an anti-hallucination
  guard and the signal a later paid-engine escalation would key on.

The parsing front-end uses pdfplumber (already a dependency); the heavier local
layout engines (Docling / Table Transformer) named in the plan are a later
escalation. The stitch/reflow/ground functions are pure and operate on a small
intermediate page representation, so they are unit-tested without a real PDF.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from .logging import get_logger

log = get_logger(__name__)


@dataclass
class StitchedTable:
    rows: list[list[str]]
    page_start: int
    page_end: int
    confidence: float = 1.0
    source_engine: str = "pdfplumber"
    needs_review: bool = False
    # Grid consistency reported by the detector. Grounding cannot judge this: a
    # locally-read table always grounds at 1.0 because its digits come from the
    # same text layer, so a ragged detection would otherwise pass as clean.
    structure_confidence: float = 1.0


@dataclass
class StructuredDoc:
    text: str = ""
    tables: list[StitchedTable] = field(default_factory=list)
    page_count: int = 0
    escalated_count: int = 0
    est_cost: float = 0.0


# --------------------------------------------------------------- intermediate
# A "page" is a dict:
#   {"page_no": int, "width": float, "height": float,
#    "lines":  [{"text": str, "x0": float, "x1": float, "top": float, "bottom": float}, ...],
#    "tables": [{"bbox": (x0, top, x1, bottom), "col_x": [float, ...],
#                "rows": [[cell|None, ...], ...],
#                "source_engine": str, "structure_confidence": float}, ...]}
# Pure functions below consume this shape, so tests can build it directly.


# ------------------------------------------------------------- table stitching
def _col_signature_matches(a: list[float], b: list[float], tol: float) -> bool:
    """Two column x-edge lists describe the same table shape."""
    if not a or not b or abs(len(a) - len(b)) > 1:
        return False
    n = min(len(a), len(b))
    return all(abs(a[i] - b[i]) <= tol for i in range(n))


def _norm_row(row: list) -> list[str]:
    return [("" if c is None else str(c).strip()) for c in row]


def stitch_tables(
    pages: list[dict],
    *,
    top_frac: float = 0.18,
    bottom_frac: float = 0.82,
    x_tol: float = 12.0,
) -> list[StitchedTable]:
    """Join tables that continue across page breaks (geometry + column signature).

    A table starting near the top of page N continues the table that ended near
    the bottom of page N-1 when their column x-signatures match; a repeated
    header row on the continuation is dropped.
    """
    stitched: list[StitchedTable] = []
    open_tbl: StitchedTable | None = None
    open_sig: list[float] = []
    open_ended_low = False
    prev_page_no: int | None = None

    for page in pages:
        height = float(page.get("height") or 0) or 1.0
        tables = page.get("tables") or []
        for ti, tbl in enumerate(tables):
            bbox = tbl.get("bbox") or (0, 0, 0, 0)
            top, bottom = float(bbox[1]), float(bbox[3])
            sig = [float(x) for x in (tbl.get("col_x") or [])]
            rows = [_norm_row(r) for r in (tbl.get("rows") or []) if r]
            if not rows:
                continue
            starts_high = top <= height * top_frac
            is_first_on_page = ti == 0
            consecutive = prev_page_no is not None and page["page_no"] == prev_page_no + 1

            if (open_tbl is not None and is_first_on_page and starts_high
                    and open_ended_low and consecutive
                    and _col_signature_matches(open_sig, sig, x_tol)):
                # continuation — drop a repeated header row if present
                body = rows
                if open_tbl.rows and body and body[0] == open_tbl.rows[0]:
                    body = body[1:]
                open_tbl.rows.extend(body)
                open_tbl.page_end = page["page_no"]
            else:
                open_tbl = StitchedTable(
                    rows=rows, page_start=page["page_no"], page_end=page["page_no"],
                    source_engine=tbl.get("source_engine") or "pdfplumber",
                    structure_confidence=float(tbl.get("structure_confidence", 1.0)),
                )
                stitched.append(open_tbl)
                open_sig = sig
            open_ended_low = bottom >= height * bottom_frac
        prev_page_no = page["page_no"]
        # A page with no table breaks any open continuation.
        if not tables:
            open_tbl = None
    return stitched


# --------------------------------------------------------------- reflow / prose
_CJK = (
    (0x2E80, 0x9FFF), (0x3000, 0x303F), (0xAC00, 0xD7A3),  # CJK + Hangul syllables
    (0xF900, 0xFAFF), (0xFF00, 0xFFEF),
)
_SENT_END = tuple(".!?…。！？」』）)")
_PAGE_NUM_RE = re.compile(r"^[\s\-–—]*(?:page|p\.?|페이지|第)?\s*\d+\s*(?:/\s*\d+)?"
                          r"\s*(?:쪽|페이지|页)?[\s\-–—]*$", re.IGNORECASE)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK)


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip().lower()


def _recurring_lines(pages: list[dict], band: float, at_top: bool) -> set[str]:
    """Normalized text of lines that recur in the top/bottom band across pages."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for page in pages:
        height = float(page.get("height") or 0) or 1.0
        for ln in page.get("lines") or []:
            top = float(ln.get("top", 0))
            in_band = top <= height * band if at_top else top >= height * (1 - band)
            if in_band:
                key = _norm_key(ln.get("text", ""))
                if key:
                    counts[key] += 1
    n_pages = len(pages)
    if n_pages < 3:
        return set()
    thresh = max(3, int(n_pages * 0.5))
    return {k for k, c in counts.items() if c >= thresh}


def _join(acc: str, nxt: str) -> str:
    """Join two text fragments, de-hyphenating and respecting CJK spacing."""
    if not acc:
        return nxt
    if not nxt:
        return acc
    if acc.endswith("-") and nxt[:1].islower():
        return acc[:-1] + nxt  # de-hyphenate a latin line-break
    if _is_cjk(acc[-1]) and _is_cjk(nxt[0]):
        return acc + nxt  # no space between CJK characters
    return acc + " " + nxt


def reflow_narrative(pages: list[dict], *, band: float = 0.12) -> str:
    """Strip running headers/footers + page numbers and merge paragraphs.

    Content-agnostic: headers/footers are found by recurrence across pages, page
    numbers by a numeric-line shape, and paragraphs are merged until a line ends
    with sentence punctuation (works for latin and CJK terminators).
    """
    drop = _recurring_lines(pages, band, at_top=True) | _recurring_lines(pages, band, at_top=False)
    paragraphs: list[str] = []
    cur = ""
    for page in pages:
        for ln in page.get("lines") or []:
            text = (ln.get("text") or "").strip()
            if not text:
                continue
            key = _norm_key(text)
            if key in drop or _PAGE_NUM_RE.match(text):
                continue
            cur = _join(cur, text)
            if text.endswith(_SENT_END):
                paragraphs.append(cur)
                cur = ""
    if cur:
        paragraphs.append(cur)
    return "\n\n".join(paragraphs).strip()


# ------------------------------------------------------------- numeric grounding
_NUM_RE = re.compile(r"-?\(?\d[\d,]*\.?\d*\)?%?")


def _digits(token: str) -> str:
    return re.sub(r"\D", "", token)


def ground_numbers(rows: list[list[str]], raw_text: str) -> float:
    """Fraction of numeric cells whose digits also appear in the raw text layer.

    1.0 when a table carries no numbers (nothing to hallucinate). Digits-only
    comparison ignores thousands separators / parentheses / percent signs so the
    check is currency- and locale-agnostic.
    """
    raw_digits = _digits(raw_text)
    total = grounded = 0
    for row in rows:
        for cell in row:
            for tok in _NUM_RE.findall(cell or ""):
                d = _digits(tok)
                if len(d) < 2:  # skip single digits (too common to be meaningful)
                    continue
                total += 1
                if d in raw_digits:
                    grounded += 1
    return 1.0 if total == 0 else grounded / total


# ----------------------------------------------------------------- pdf front-end
def _parse_pages(pdf, fallback: str | None = "column-geometry",
                 ml_dpi: int = 150) -> list[dict]:
    pages: list[dict] = []
    for page in pdf.pages:
        rep: dict = {
            "page_no": page.page_number,
            "width": float(page.width),
            "height": float(page.height),
            "lines": [],
            "tables": [],
        }
        try:
            for ln in page.extract_text_lines(strip=True):
                rep["lines"].append({
                    "text": ln.get("text", ""), "x0": ln.get("x0", 0.0),
                    "x1": ln.get("x1", 0.0), "top": ln.get("top", 0.0),
                    "bottom": ln.get("bottom", 0.0),
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("extract_text_lines failed on p%s: %s", page.page_number, exc)
        try:
            for tbl in page.find_tables():
                col_x = []
                try:
                    col_x = [float(c.bbox[0]) for c in tbl.columns]
                except Exception as exc:  # noqa: BLE001
                    log.debug("column bbox read failed: %s", exc)
                rep["tables"].append({
                    "bbox": tuple(float(v) for v in tbl.bbox),
                    "col_x": col_x, "rows": tbl.extract(),
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("find_tables failed on p%s: %s", page.page_number, exc)
        if not rep["tables"]:
            # No ruling lines here. Results announcements and annual reports hold
            # their columns apart with whitespace, so fall back to reconstructing
            # the grid — or to a model, when the ML tier is switched on. Either
            # way only on pages the ruled pass left empty, so a document it
            # already handles cannot regress.
            try:
                if fallback == "table-transformer":
                    from .pdf_ml_tables import find_tatr_tables

                    rep["tables"].extend(find_tatr_tables(page, dpi=ml_dpi))
                elif fallback == "column-geometry":
                    from .pdf_columns import find_column_tables

                    rep["tables"].extend(find_column_tables(page))
                # fallback None: Docling supplies these pages at document level
            except Exception as exc:  # noqa: BLE001
                log.debug("fallback table detection failed on p%s: %s", page.page_number, exc)
        pages.append(rep)
    return pages


def _ml_usable(engine: str) -> bool:
    from .pdf_ml_tables import ml_ready

    return ml_ready(engine)


def _docling_tables(content: bytes):
    from .pdf_ml_tables import find_docling_tables

    try:
        return find_docling_tables(content)
    except Exception as exc:  # noqa: BLE001
        log.warning("docling conversion failed, falling back to the built-in detectors: %s", exc)
        return None


def _page_text_map(pages: list[dict]) -> dict[int, str]:
    return {p["page_no"]: "\n".join(ln["text"] for ln in p["lines"]) for p in pages}


def apply_escalation(tables: list[StitchedTable], page_text: dict[int, str], raw_text: str,
                     *, escalator, threshold: float, cost_per_page: float) -> tuple[int, float]:
    """Flag + optionally re-process low-confidence tables. Returns (count, est_cost).

    Grounding is the meaningful anti-hallucination check for *escalation output*
    (a paid engine could invent numbers absent from the page), so escalated rows
    are re-grounded against the page's raw text; the local engine, which reads the
    same text layer, is already self-consistent.
    """
    from .pdf_escalate import NullEscalator

    use = escalator is not None and not isinstance(escalator, NullEscalator)
    escalated = 0
    est_cost = 0.0
    for t in tables:
        if t.confidence >= threshold:
            continue
        t.needs_review = True
        if not use:
            continue
        context = "\n".join(page_text.get(pg, "") for pg in range(t.page_start, t.page_end + 1))
        col_count = max((len(r) for r in t.rows), default=0)
        new_rows = escalator.escalate(t.rows, context, col_count)
        est_cost += cost_per_page * (t.page_end - t.page_start + 1)
        escalated += 1
        if new_rows:
            t.rows = new_rows
            t.confidence = ground_numbers(new_rows, raw_text)
            t.source_engine = getattr(escalator, "name", "escalated")
            t.needs_review = t.confidence < threshold
    return escalated, est_cost


def extract_structured(content: bytes, escalator=None, threshold: float = 0.66,
                       cost_per_page: float = 0.0, ml_engine: str | None = None,
                       ml_dpi: int = 150) -> StructuredDoc:
    """Parse a PDF into stitched tables + reflowed narrative with grounding.

    Tables below ``threshold`` grounding confidence are flagged ``needs_review``.
    When an ``escalator`` is given (see ``pdf_escalate``), each low-confidence
    table is re-processed by it; on success the rows are replaced, re-grounded,
    the ``source_engine`` updated and the review flag cleared. Escalation cost is
    accounted per escalated table span.

    Returns an empty doc (not an error) for a scanned/image-only PDF that yields
    no text layer — OCR is a later escalation.
    """
    import pdfplumber

    if ml_engine and not _ml_usable(ml_engine):
        ml_engine = None                    # warned once; fall back to the built-ins

    docling_tables = None
    if ml_engine == "docling":
        docling_tables = _docling_tables(content)
    # An engine that was asked for owns the non-ruled pages: running the geometry
    # pass as well would fill those pages first and the requested engine's tables
    # would never be reached.
    fallback = {"table-transformer": "table-transformer",
                "docling": None}.get(ml_engine, "column-geometry")
    if ml_engine == "docling" and docling_tables is None:
        fallback = "column-geometry"        # conversion failed; keep the built-in

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        pages = _parse_pages(pdf, fallback, ml_dpi)
    if docling_tables:
        # Docling converts whole documents, so its tables arrive keyed by page
        # rather than through the per-page hook.
        for rep in pages:
            if not rep["tables"]:
                rep["tables"].extend(docling_tables.get(rep["page_no"], []))
    raw_text = "\n".join(ln["text"] for p in pages for ln in p["lines"])
    page_text = _page_text_map(pages)
    tables = stitch_tables(pages)
    for t in tables:
        # Grounding catches invented numbers, structure catches a bad grid; a
        # table has to satisfy both, so the weaker one decides.
        t.confidence = min(ground_numbers(t.rows, raw_text), t.structure_confidence)

    escalated, est_cost = apply_escalation(
        tables, page_text, raw_text,
        escalator=escalator, threshold=threshold, cost_per_page=cost_per_page,
    )

    text = reflow_narrative(pages)
    return StructuredDoc(text=text, tables=tables, page_count=len(pages),
                         escalated_count=escalated, est_cost=est_cost)
