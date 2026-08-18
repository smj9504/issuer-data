"""OCR fallback for scanned / image-only PDFs.

pdfplumber recovers nothing from a PDF with no text layer (older HKEXnews
filings, scanned government documents). When OCR is enabled, each page is
rendered to an image (PyMuPDF) and passed through Tesseract, which is multilingual
(``eng+kor+chi_tra`` by default), so Korean / English / Traditional-Chinese scans
all yield text.

OCR is on by default. The Python side is a runtime dependency, but the Tesseract
binary is a system package, so ``ocr_ready`` reports whether the whole stack is
usable and says once — not per page — what is missing when it is not.
"""

from __future__ import annotations

import io
from functools import lru_cache

from .logging import get_logger

log = get_logger(__name__)


_INSTALL_HINT = (
    "install the Python side with `pip install '.[ocr]'` and the engine with "
    "`apt install tesseract-ocr tesseract-ocr-kor tesseract-ocr-chi-tra`, "
    "or set ISSUER_OCR_ENABLED=false / pass --no-ocr to stop trying"
)


def _pymupdf():
    """PyMuPDF under either module name — `fitz` is deprecated but still shipped."""
    try:
        import pymupdf
    except ImportError:  # PyMuPDF < 1.24.3 only exposes the `fitz` name
        import fitz as pymupdf
    return pymupdf


def _open_pdf(content: bytes):
    return _pymupdf().open(stream=content, filetype="pdf")


def ocr_available() -> bool:
    """True when PyMuPDF, Pillow, pytesseract, and the Tesseract binary are usable."""
    try:
        import pytesseract
        from PIL import Image  # noqa: F401

        _pymupdf()
    except ImportError:
        return False
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 - the binary is missing or not runnable
        return False
    return True


@lru_cache(maxsize=1)
def ocr_ready() -> bool:
    """`ocr_available()`, warning once per process when the stack is unusable.

    Without this, an enabled-by-default OCR that cannot run would either fail
    silently or repeat the same error for every page of every scanned document.
    """
    if ocr_available():
        return True
    log.warning("OCR is enabled but unavailable, scanned PDFs will have no text — %s",
                _INSTALL_HINT)
    return False


def ocr_pages(content: bytes, languages: str = "eng", dpi: int = 300,
              max_pages: int = 50, only: set[int] | None = None) -> dict[int, str]:
    """OCR selected pages; returns {0-based page index: text}.

    `only` limits the work to the pages that need it. A filing is often mostly
    born-digital with a few scanned or chart-only pages, and re-OCRing the pages
    that already carry a text layer would be both slow and worse than the text
    that is already there.
    """
    import pytesseract
    from PIL import Image

    out: dict[int, str] = {}
    doc = _open_pdf(content)
    try:
        done = 0
        for i, page in enumerate(doc):
            if only is not None and i not in only:
                continue
            if done >= max_pages:
                log.info("OCR page cap (%d) reached; remaining pages skipped", max_pages)
                break
            done += 1
            try:
                pix = page.get_pixmap(dpi=dpi)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                text = pytesseract.image_to_string(img, lang=languages).strip()
                if text:
                    out[i] = text
            except Exception as exc:  # noqa: BLE001
                log.warning("OCR failed on page %d: %s", i + 1, exc)
    finally:
        doc.close()
    return out


def ocr_pdf(content: bytes, languages: str = "eng", dpi: int = 300,
            max_pages: int = 50) -> str | None:
    """Render every PDF page and OCR it; return the concatenated text (or None)."""
    pages = ocr_pages(content, languages, dpi, max_pages)
    text = "\n".join(pages[i] for i in sorted(pages)).strip()
    return text or None
