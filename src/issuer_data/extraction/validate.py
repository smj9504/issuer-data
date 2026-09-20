"""Reference-free validation: does this extraction contradict itself?

Accuracy against an arbitrary PDF cannot be proven without a label, but *failure*
can be detected without one. Three checks here need no ground truth at all, so
they run on every document:

- **Coverage** — what fraction of the page's own content survived into the output.
  ``ground_numbers`` (pdf_extract) asks "did we invent a number?"; it cannot ask
  "did we drop half the table?", because a locally-read table always grounds at
  1.0 — its digits come from the same text layer. Coverage is the other half of
  that question and catches the silent-loss failures grounding is blind to.
- **Arithmetic** — financial tables check themselves. When a 합계 / Total row
  equals the sum of the rows above it, the parse of that column is almost
  certainly right: a shifted cell or a merged column breaks the identity
  immediately. This is real evidence, not a heuristic.
- **Required fields** — "was this PDF parsed correctly" is unbounded; "did we get
  the fields we came for, each with a page to look at" is answerable. Enforced
  only when the caller supplies a schema (see ``pdf_fields``).

The three fold into one document verdict — PASS / REVIEW / FAIL — because a
silent success is the failure mode that costs the most: a document that yields
nothing must be *loud*, not empty. Every verdict carries the reasons that
produced it, so a reviewer starts from "this table's total is off by 12" rather
than from a whole PDF.

A check that could not run never counts as a failure: a table with no total row
reports zero attempted checks, not a failed one.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .logging import get_logger
from .pdf_extract import content_lines

log = get_logger(__name__)

PASS = "pass"
REVIEW = "review"
FAIL = "fail"


# ------------------------------------------------------------------- tokenizing
# One token per latin word, per digit run, and per CJK/Hangul character — CJK is
# not space-delimited, so whitespace tokenizing would make one token of a whole
# line and coverage would swing between 0 and 1 with a single edit.
_TOKEN_RE = re.compile(
    r"[0-9]+"                                    # a digit run
    r"|[A-Za-z\u00C0-\u024F]+"                   # a latin word
    r"|[\u1100-\u11FF\u3040-\u30FF\u3130-\u318F\u3400-\u4DBF\u4E00-\u9FFF"
    r"\uA960-\uA97F\uAC00-\uD7FF\uF900-\uFAFF]"  # one CJK / Hangul character
)
# Deliberately not allowed to span spaces: merging "12 34" into "1234" would
# invent a number the output never has to match, and coverage would cry wolf.
_NUM_TOKEN_RE = re.compile(r"\d[\d,.]*\d|\d")
_THIN_SPACE_RE = re.compile(r"[\u00a0\u2007\u2009\u202f]")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _number_keys(text: str) -> list[str]:
    """Digit-only keys for the numbers in a string (>= 2 digits).

    Single digits are skipped: they recur everywhere and would drown the signal.
    Separators are stripped so 1,234 / 1 234 / 1.234 compare equal.
    """
    out: list[str] = []
    for tok in _NUM_TOKEN_RE.findall(_THIN_SPACE_RE.sub(" ", text or "")):
        digits = re.sub(r"\D", "", tok)
        if len(digits) >= 2:
            out.append(digits)
    return out


def _recall(expected: list[str], got: list[str]) -> tuple[float, Counter]:
    """Multiset recall of `expected` within `got`, plus what is missing."""
    want, have = Counter(expected), Counter(got)
    if not want:
        return 1.0, Counter()
    missing = want - have
    return (sum(want.values()) - sum(missing.values())) / sum(want.values()), missing


# --------------------------------------------------------------------- coverage
@dataclass
class Coverage:
    """How much of the page's own content reached the output."""

    # None when there was nothing to measure — a document with no text layer has
    # not "recalled 100%", it has nothing to recall, and reporting 1.0 next to a
    # FAIL reads as though the extraction went fine.
    token_recall: float | None = 1.0
    numeric_recall: float | None = 1.0
    source_tokens: int = 0
    source_numbers: int = 0
    missing_numbers: list[str] = field(default_factory=list)
    lost_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "token_recall": None if self.token_recall is None else round(self.token_recall, 4),
            "numeric_recall": (None if self.numeric_recall is None
                               else round(self.numeric_recall, 4)),
            "source_tokens": self.source_tokens,
            "source_numbers": self.source_numbers,
            "missing_numbers": self.missing_numbers[:20],
            "lost_lines": self.lost_lines[:10],
        }


def coverage(pages: list[dict], text: str, tables) -> Coverage:
    """Fraction of the source's content tokens/numbers present in the output.

    The output is the reflowed narrative *plus* every table cell, since a line
    that became a table row is not lost. The baseline is ``content_lines`` — what
    the engine meant to keep — so deliberate header/footer removal scores clean.
    """
    kept = content_lines(pages)
    src_text = "\n".join(t for _pg, t in kept)
    out_text = "\n".join(
        [text or ""] + [str(c or "") for t in (tables or []) for r in t.rows for c in r]
    )

    src_tokens, out_tokens = _tokens(src_text), _tokens(out_text)
    src_nums, out_nums = _number_keys(src_text), _number_keys(out_text)
    token_recall, _ = _recall(src_tokens, out_tokens)
    numeric_recall, missing_nums = _recall(src_nums, out_nums)

    # Name the lines that vanished whole — a reviewer can jump straight to them.
    out_token_pool = Counter(out_tokens)
    lost: list[str] = []
    for _pg, line in kept:
        toks = _tokens(line)
        if toks and not any(out_token_pool.get(t) for t in toks):
            lost.append(line)

    return Coverage(
        token_recall=token_recall if src_tokens else None,
        numeric_recall=numeric_recall if src_nums else None,
        source_tokens=len(src_tokens),
        source_numbers=len(src_nums),
        missing_numbers=sorted(missing_nums.elements())[:20],
        lost_lines=lost[:10],
    )


# ------------------------------------------------------------------- arithmetic
# A cell holding exactly one of these is a total for the rows above it. Matched
# whole (after stripping spaces/punctuation) so '계정'/'Total assets' do not hit:
# a label that merely *contains* '계' is not a total row.
_TOTAL_LABELS = {
    "합계", "합 계", "계", "총계", "총액", "소계", "누계", "합",
    "total", "totals", "subtotal", "subtotals", "sum", "grandtotal",
    "合計", "總計", "总计", "小計", "小计", "總額", "总额",
}
# Nil markers. In a financial table a dash means zero, and reading it as "no
# value" would silently drop a term and break every sum it takes part in.
_NIL = {"-", "–", "—", "―", "‐", "", "n/a", "na", "nil", "없음", "해당없음"}
_NEG_PREFIX = ("△", "▲", "Δ", "-", "−", "–", "—")
_DATE_RE = re.compile(r"^\d{4}[-./]\d{1,2}([-./]\d{1,2})?$"
                      r"|^\d{1,2}[-./]\d{1,2}[-./]\d{2,4}$")
_WORDY_RE = re.compile(r"[A-Za-z\u3040-\u9FFF\uAC00-\uD7A3]")


def parse_number(cell) -> float | None:
    """Parse a table cell into a number, or None when it is not numeric.

    Handles the notations these filings actually use: thousands separators,
    (1,234) and △1,234 for negatives — the Korean convention — currency symbols,
    trailing units, and a dash for nil.
    """
    if cell is None:
        return None
    raw = _THIN_SPACE_RE.sub(" ", str(cell)).strip()
    if raw.lower() in _NIL:
        return 0.0
    if _DATE_RE.match(raw):
        return None                # a period column holds dates, and dates do not sum
    neg = False
    if raw.startswith("(") and raw.endswith(")"):
        neg, raw = True, raw[1:-1].strip()
    while raw[:1] in _NEG_PREFIX:
        neg, raw = not neg, raw[1:].strip()
    first = re.search(r"\d", raw)
    if not first:
        return None
    # Letters *before* the first digit mean this is a label, not a value:
    # '매출액 1,234' and 'FY2023' are not numeric cells. A trailing unit
    # ('1,234 백만원') is fine and is kept.
    if _WORDY_RE.search(raw[:first.start()]):
        return None
    body = re.sub(r"[^\d.,]", "", raw)
    if not body:
        return None
    body = body.replace(",", "")
    if body.count(".") > 1:        # 1.234.567 — European grouping, not a decimal
        body = body.replace(".", "")
    try:
        value = float(body)
    except ValueError:
        return None
    return -value if neg else value


def _is_total_label(cell) -> bool:
    norm = re.sub(r"[\s.·:()\[\]]+", "", str(cell or "")).lower()
    return norm in _TOTAL_LABELS


@dataclass
class Arithmetic:
    """Result of the self-checking sums found in a table."""

    checks: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        """None when the table carried no total to check — not a failure."""
        return None if not self.checks else self.passed / self.checks

    def to_dict(self) -> dict:
        return {"checks": self.checks, "passed": self.passed,
                "score": None if self.score is None else round(self.score, 4),
                "failures": self.failures[:10]}


def _close(total: float, parts: list[float], rel_tol: float) -> bool:
    """Equal within rounding: each term may be rounded, so error accumulates."""
    got = sum(parts)
    tol = max(rel_tol * abs(total), 0.5 * len(parts) + 1e-9)
    return abs(total - got) <= tol


def _header_index(rows: list[list[str]]) -> int:
    """Index of a leading header row, or -1.

    Financial headers put a year in the value columns ('계정 | 2023 | 2022'), and
    a year parses as a perfectly good number — left in the terms it breaks every
    total it takes part in.
    """
    if not rows or not rows[0]:
        return -1
    label = str(rows[0][0] or "").strip()
    if _is_total_label(label):
        return -1
    return 0 if (not label or parse_number(label) is None) else -1


def _terms(column: list, lo: int, hi: int, skip: set[int], header: int) -> list[float]:
    """The numeric terms in ``column[lo:hi]``, minus the total rows and, when
    ``header`` is not -1, the header row."""
    return [v for j, v in enumerate(column[lo:hi], start=lo)
            if v is not None and j not in skip and j != header]


def check_arithmetic(rows: list[list[str]], *, rel_tol: float = 0.005) -> Arithmetic:
    """Verify every 합계/Total row against the rows it totals.

    Several readings are accepted, because a table may carry subtotals and may or
    may not have a header row the detector kept: the total may sum everything
    above it or only the block since the previous total, with or without the
    header. Any match passes — the job is to catch cells that shifted, not to
    police layout, and a genuinely misparsed total matches none of the readings.
    """
    report = Arithmetic()
    if len(rows) < 3:
        return report
    width = max((len(r) for r in rows), default=0)
    if width < 2:
        return report

    total_rows = [i for i, r in enumerate(rows) if any(_is_total_label(c) for c in r[:2])]
    if not total_rows:
        return report
    skip = set(total_rows)
    header = _header_index(rows)

    for col in range(1, width):
        column = [parse_number(r[col]) if col < len(r) else None for r in rows]
        prev_total = -1
        drops = (False, True) if header >= 0 else (False,)
        for i in total_rows:
            total = column[i]
            if total is None:
                continue
            readings = [_terms(column, lo, i, skip, header if drop else -1)
                        for lo in (prev_total + 1, 0) for drop in drops]
            prev_total = i
            readings = [r for r in readings if r]
            if not readings:
                continue
            report.checks += 1
            if any(_close(total, reading, rel_tol) for reading in readings):
                report.passed += 1
            else:
                label = next((str(c) for c in rows[i][:2] if str(c or "").strip()), "?")
                report.failures.append(
                    f"row {i} ({label}) col {col}: stated {total:g}, "
                    f"rows above sum to {sum(readings[0]):g}"
                )
    return report


def check_tables_arithmetic(tables, *, rel_tol: float = 0.005) -> Arithmetic:
    """Aggregate the per-table arithmetic checks across a document."""
    combined = Arithmetic()
    for seq, table in enumerate(tables or []):
        one = check_arithmetic(table.rows, rel_tol=rel_tol)
        combined.checks += one.checks
        combined.passed += one.passed
        combined.failures.extend(f"table {seq}: {f}" for f in one.failures)
    return combined


# ------------------------------------------------------------------ the verdict
@dataclass
class ValidationReport:
    """Document-level verdict plus the evidence behind it."""

    verdict: str = PASS
    reasons: list[str] = field(default_factory=list)
    coverage: Coverage | None = None
    arithmetic: Arithmetic = field(default_factory=Arithmetic)
    tables_total: int = 0
    tables_flagged: int = 0
    fields: dict = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    agreement: float | None = None
    crosscheck: dict | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == PASS

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reasons": self.reasons,
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "arithmetic": self.arithmetic.to_dict(),
            "tables_total": self.tables_total,
            "tables_flagged": self.tables_flagged,
            "fields": {k: v.to_dict() if hasattr(v, "to_dict") else v
                       for k, v in (self.fields or {}).items()},
            "missing_fields": self.missing_fields,
            "agreement": None if self.agreement is None else round(self.agreement, 4),
            "crosscheck": self.crosscheck,
        }


@dataclass
class Thresholds:
    """Where each signal stops being acceptable. Calibrate these on gold data
    (``python -m issuer_data eval``) rather than guessing — the numbers below are
    a starting point, not a measurement."""

    coverage_min: float = 0.98      # below → review
    coverage_fail: float = 0.85     # below → fail: content was lost wholesale
    arithmetic_min: float = 1.0     # any total that does not add up is worth a look
    agreement_min: float = 0.90
    crosscheck_min: float = 1.0
    rel_tol: float = 0.005


def decide(report: ValidationReport, thresholds: Thresholds | None = None) -> ValidationReport:
    """Reduce the gathered signals to one verdict, in place.

    Separate from gathering so a signal that arrives later — cross-source
    agreement needs a database, engine consensus needs a second parse — can be
    attached and the verdict recomputed without re-parsing the PDF.

    FAIL is reserved for "there is nothing usable here": no text at all, content
    lost wholesale, or a required field missing. REVIEW means a signal fired and a
    human should look. PASS means every check that could run, ran clean — it never
    means the document is guaranteed correct, only that nothing contradicted it.
    """
    th = thresholds or Thresholds()
    cov = report.coverage
    fails: list[str] = []
    reviews: list[str] = []

    if cov is not None:
        if not cov.source_tokens:
            fails.append("no text layer: the document yielded no readable content")
        elif cov.token_recall < th.coverage_fail:
            fails.append(f"coverage {cov.token_recall:.1%} of source text "
                         f"(below the {th.coverage_fail:.0%} floor) — content was lost")
        elif (cov.token_recall < th.coverage_min
              or (cov.numeric_recall is not None and cov.numeric_recall < th.coverage_min)):
            numbers = ("n/a" if cov.numeric_recall is None
                       else f"{cov.numeric_recall:.1%}")
            reviews.append(f"coverage below target: text {cov.token_recall:.1%}, "
                           f"numbers {numbers}")
    if report.missing_fields:
        fails.append("required field(s) not found: " + ", ".join(report.missing_fields))

    score = report.arithmetic.score
    if score is not None and score < th.arithmetic_min:
        reviews.append(f"{report.arithmetic.checks - report.arithmetic.passed} of "
                       f"{report.arithmetic.checks} total row(s) do not add up")
    if report.tables_flagged:
        reviews.append(f"{report.tables_flagged} of {report.tables_total} "
                       f"table(s) below the confidence threshold")
    if report.agreement is not None and report.agreement < th.agreement_min:
        reviews.append(f"independent detectors agree only {report.agreement:.1%} "
                       f"on the extracted tables")
    cross = report.crosscheck or {}
    if cross.get("conflicts"):
        reviews.append(f"{len(cross['conflicts'])} figure(s) contradict data "
                       f"collected from other sources")

    if fails:
        report.verdict, report.reasons = FAIL, fails + reviews
    elif reviews:
        report.verdict, report.reasons = REVIEW, reviews
    else:
        report.verdict, report.reasons = PASS, []
    return report


def validate(pages, text, tables, *, thresholds: Thresholds | None = None,
             field_specs=None, agreement: float | None = None,
             crosscheck: dict | None = None) -> ValidationReport:
    """Run every reference-free check on one document and return its verdict."""
    th = thresholds or Thresholds()
    report = ValidationReport(tables_total=len(tables or []))
    report.tables_flagged = sum(1 for t in (tables or []) if getattr(t, "needs_review", False))
    report.coverage = coverage(pages, text, tables)
    report.arithmetic = check_tables_arithmetic(tables, rel_tol=th.rel_tol)
    report.agreement = agreement
    report.crosscheck = crosscheck

    if field_specs:
        from .pdf_fields import extract_fields

        report.fields = extract_fields(text, tables, field_specs)
        report.missing_fields = [s.name for s in field_specs
                                 if s.required and not report.fields.get(s.name)]
    return decide(report, th)
