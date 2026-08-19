"""Engine consensus: two independent detectors as each other's referee.

With no ground truth, agreement is the next best evidence. Two detectors that do
not share assumptions — one reading ruling lines, one inferring the grid from
where the words sit — fail on different documents. When both return the same
grid, the odds that both went wrong the same way are small; when they diverge,
something on that page is ambiguous and a person should look.

This is the same TEDS used by the eval harness (``eval.metrics``), pointed at
prediction-vs-prediction instead of prediction-vs-gold — so it needs no labelled
data and works on the first document a new issuer ever files.

It costs a second full parse, so it is opt-in (``--agreement`` / config), meant
for spot checks and sampling rather than every document in a bulk backfill.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .eval import metrics
from .logging import get_logger

log = get_logger(__name__)


@dataclass
class TableAgreement:
    table_seq: int
    page_start: int
    page_end: int
    score: float
    matched: bool          # False when the other detector found nothing there


@dataclass
class AgreementReport:
    score: float | None = None          # None when neither detector found a table
    per_table: list[TableAgreement] = field(default_factory=list)
    reference_engine: str = "lines"
    other_engine: str = "text"
    other_table_count: int = 0

    def to_dict(self) -> dict:
        return {
            "score": None if self.score is None else round(self.score, 4),
            "reference_engine": self.reference_engine,
            "other_engine": self.other_engine,
            "other_table_count": self.other_table_count,
            "per_table": [{"table_seq": t.table_seq, "pages": [t.page_start, t.page_end],
                           "score": round(t.score, 4), "matched": t.matched}
                          for t in self.per_table],
        }


def _overlaps(a, b) -> bool:
    return a.page_start <= b.page_end and b.page_start <= a.page_end


def dense(rows: list[list[str]]) -> list[list[str]]:
    """Drop rows and columns that hold nothing, on both sides of the comparison.

    The text-geometry detector emits a blank spacer row for the whitespace
    between printed rows; the line detector does not. That is a difference in how
    each draws the grid, not a disagreement about content — and left in, it scores
    a perfectly matching table at ~0.59, so every clean document would be flagged.
    An alarm that always fires is one people learn to ignore, so the comparison is
    made on content and content alone.
    """
    kept = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not kept:
        return []
    width = max(len(r) for r in kept)
    padded = [list(r) + [""] * (width - len(r)) for r in kept]
    columns = [i for i in range(width) if any(str(r[i] or "").strip() for r in padded)]
    return [[row[i] for i in columns] for row in padded]


def compare_tables(reference, other) -> AgreementReport:
    """Best-TEDS match for each reference table among page-overlapping candidates.

    Restricting candidates to overlapping pages stops a table on page 40 from
    being 'confirmed' by a similarly shaped table on page 3.
    """
    report = AgreementReport(other_table_count=len(other or []))
    if not reference:
        # Nothing to defend, but the other engine finding tables here is itself
        # worth knowing: 0.0 says "one of us missed everything".
        report.score = None if not other else 0.0
        return report
    for seq, table in enumerate(reference):
        candidates = [o for o in (other or []) if _overlaps(table, o)]
        target = dense(table.rows)
        best = max((metrics.teds(dense(o.rows), target) for o in candidates), default=0.0)
        report.per_table.append(TableAgreement(
            table_seq=seq, page_start=table.page_start, page_end=table.page_end,
            score=best, matched=bool(candidates),
        ))
    report.score = sum(t.score for t in report.per_table) / len(report.per_table)
    return report


def agreement(content: bytes, *, reference=None, other_detector: str = "text",
              ml_engine: str | None = None, ml_dpi: int = 150,
              flag_below: float | None = None) -> AgreementReport:
    """Re-parse `content` with an independent detector and score the overlap.

    Pass the already-parsed tables as ``reference`` to avoid parsing twice. When
    ``flag_below`` is given, reference tables scoring under it are marked
    ``needs_review`` in place — disagreement is routed to a human the same way a
    low confidence is.
    """
    from .pdf_extract import extract_structured

    if reference is None:
        reference = extract_structured(content, ml_engine=ml_engine, ml_dpi=ml_dpi,
                                       validate=False).tables
    try:
        second = extract_structured(content, detector=other_detector, validate=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("second-opinion parse failed (%s): %s", other_detector, exc)
        return AgreementReport(score=None, other_engine=other_detector)

    report = compare_tables(reference, second.tables)
    report.other_engine = other_detector
    if flag_below is not None:
        for entry in report.per_table:
            if entry.score < flag_below:
                table = reference[entry.table_seq]
                table.needs_review = True
    return report
