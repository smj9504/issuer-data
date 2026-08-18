"""Optional ML table detectors: Microsoft Table Transformer and IBM Docling.

Both are MIT-licensed and run entirely on this machine — no API, no per-page
cost. What they cost is weight: torch and its CUDA wheels, a few GB on disk, and
CPU inference measured in seconds per page rather than milliseconds. That is why
they are an opt-in tier (`pip install '.[ml]'`, `--ml-tables`) rather than the
default, and why nothing here is imported until it is actually asked for.

Where they sit: `pdf_extract` tries ruling lines first (cheap, exact when the
document draws them). On pages with no ruled table it falls back to the
whitespace-geometry pass, or — when this tier is on — to one of these models
instead. The text still comes from the PDF's own text layer; the model is asked
only where the cell boundaries are, so nothing is invented by an OCR pass and
the numbers stay exactly as filed.

`table-transformer` works page by page and slots into the same per-page hook as
the other detectors. `docling` converts a whole document at once, so it is
driven at document level and its tables are handed back keyed by page.
"""

from __future__ import annotations

from functools import lru_cache

from .logging import get_logger

log = get_logger(__name__)

ENGINES = ("table-transformer", "docling")

_DETECTION_MODEL = "microsoft/table-transformer-detection"
_STRUCTURE_MODEL = "microsoft/table-transformer-structure-recognition"
_RENDER_DPI = 150
_MIN_SCORE = 0.6

_INSTALL_HINT = (
    "install the ML table tier with `pip install '.[ml]'` (torch + transformers, "
    "several GB), or drop --ml-tables to stay on the built-in detectors"
)


def ml_available(engine: str) -> bool:
    """Whether `engine`'s Python stack is importable."""
    try:
        if engine == "table-transformer":
            import torch  # noqa: F401
            import transformers  # noqa: F401
        elif engine == "docling":
            import docling  # noqa: F401
        else:
            return False
    except ImportError:
        return False
    return True


@lru_cache(maxsize=len(ENGINES))
def ml_ready(engine: str) -> bool:
    """`ml_available`, warning once per engine instead of once per page."""
    if engine not in ENGINES:
        log.warning("unknown ML table engine %r; known engines: %s", engine, ", ".join(ENGINES))
        return False
    if ml_available(engine):
        return True
    log.warning("ML table engine %r requested but not installed — %s", engine, _INSTALL_HINT)
    return False


# ------------------------------------------------------------ table transformer
@lru_cache(maxsize=1)
def _tatr_models():
    """Load the detection and structure models once (weights are cached by HF)."""
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    log.info("loading Table Transformer weights (first run downloads them)")
    return (
        AutoImageProcessor.from_pretrained(_DETECTION_MODEL),
        AutoModelForObjectDetection.from_pretrained(_DETECTION_MODEL),
        AutoImageProcessor.from_pretrained(_STRUCTURE_MODEL),
        AutoModelForObjectDetection.from_pretrained(_STRUCTURE_MODEL),
    )


def find_tatr_tables(page, *, dpi: int = _RENDER_DPI, min_score: float = _MIN_SCORE) -> list[dict]:
    """Tables on `page` located by Table Transformer, filled from the text layer."""
    words = page.extract_words(keep_blank_chars=False)
    if not words:
        return []
    image, scale = _render(page, dpi)
    det_proc, det_model, str_proc, str_model = _tatr_models()

    boxes = _detect(image, det_proc, det_model, min_score, want={"table", "table rotated"})
    out: list[dict] = []
    for box, score in boxes:
        crop = image.crop(box)
        parts = _detect(crop, str_proc, str_model, min_score,
                        want={"table row", "table column"}, collect_labels=True)
        rows = sorted((b for b, _s, lab in parts if lab == "table row"), key=lambda b: b[1])
        cols = sorted((b for b, _s, lab in parts if lab == "table column"), key=lambda b: b[0])
        if len(rows) < 2 or len(cols) < 2:
            continue
        scores = [s for _b, s, _l in parts] or [score]
        grid = _fill_from_text_layer(words, box, rows, cols, scale)
        if not any(any(cell for cell in r) for r in grid):
            continue
        out.append({
            "bbox": tuple(v / scale for v in box),
            "col_x": [box[0] / scale + c[0] / scale for c in cols],
            "rows": grid,
            "source_engine": "table-transformer",
            "structure_confidence": sum(scores) / len(scores),
        })
    return out


def _render(page, dpi: int):
    """Rasterise a pdfplumber page; returns the image and its pixels-per-point."""

    scale = dpi / 72.0
    pil = page.to_image(resolution=dpi).original
    return pil.convert("RGB") if pil.mode != "RGB" else pil, scale


def _detect(image, processor, model, min_score, want, collect_labels=False):
    import torch

    inputs = processor(images=image, return_tensors="pt")
    with torch.no_grad():
        outputs = model(**inputs)
    size = torch.tensor([[image.size[1], image.size[0]]])
    result = processor.post_process_object_detection(outputs, threshold=min_score,
                                                     target_sizes=size)[0]
    found = []
    for score, label, box in zip(result["scores"], result["labels"], result["boxes"]):
        name = model.config.id2label[int(label)]
        if name not in want:
            continue
        coords = tuple(float(v) for v in box.tolist())
        found.append((coords, float(score), name) if collect_labels else (coords, float(score)))
    return found


def _fill_from_text_layer(words, table_box, rows, cols, scale) -> list[list[str]]:
    """Place the page's own words into the model's row × column grid.

    The model contributes geometry only. Taking the text from the PDF instead of
    from OCR keeps filed numbers byte-exact.

    A word inside a row goes to the nearest column rather than only to one whose
    box contains it. The predicted boxes sit a little inside the ink, so strict
    containment silently drops the first word of a label ("Gross profit" arriving
    as "profit") and, where the rightmost column is predicted narrow, an entire
    column of figures.
    """
    spans = [((table_box[0] + c[0]) / scale, (table_box[0] + c[2]) / scale) for c in cols]
    grid: list[list[str]] = []
    for row in rows:
        cells: list[list[str]] = [[] for _ in cols]
        top = (table_box[1] + row[1]) / scale
        bottom = (table_box[1] + row[3]) / scale
        for w in words:
            centre_y = (w["top"] + w["bottom"]) / 2
            if not (top <= centre_y <= bottom):
                continue
            centre_x = (w["x0"] + w["x1"]) / 2
            cells[_nearest_column(centre_x, spans)].append(w["text"])
        grid.append([" ".join(c) for c in cells])
    return [r for r in grid if any(c.strip() for c in r)]


def _nearest_column(x: float, spans: list[tuple[float, float]]) -> int:
    """Index of the column containing `x`, or of the closest one when none does."""
    return min(range(len(spans)),
               key=lambda i: max(spans[i][0] - x, x - spans[i][1], 0.0))


# ------------------------------------------------------------------------ docling
def find_docling_tables(content: bytes) -> dict[int, list[dict]]:
    """Convert a whole PDF with Docling; tables keyed by 1-based page number.

    Docling is a document converter rather than a page detector, so it cannot use
    the per-page hook the other engines share.
    """
    import io

    from docling.datamodel.base_models import DocumentStream
    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(
        DocumentStream(name="filing.pdf", stream=io.BytesIO(content))
    )
    by_page: dict[int, list[dict]] = {}
    for table in getattr(result.document, "tables", []):
        rows = _docling_grid(table)
        if not rows:
            continue
        page_no = _docling_page(table)
        by_page.setdefault(page_no, []).append({
            "bbox": (0.0, 0.0, 0.0, 0.0),
            "col_x": [],
            "rows": rows,
            "source_engine": "docling",
            "structure_confidence": 1.0,
        })
    return by_page


def _docling_grid(table) -> list[list[str]]:
    grid = getattr(getattr(table, "data", None), "grid", None)
    if not grid:
        return []
    return [[(getattr(cell, "text", "") or "").strip() for cell in row] for row in grid]


def _docling_page(table) -> int:
    for prov in getattr(table, "prov", []) or []:
        page_no = getattr(prov, "page_no", None)
        if page_no:
            return int(page_no)
    return 1
