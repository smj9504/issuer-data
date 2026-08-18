"""OCR fallback for scanned / image-only PDFs.

pdfplumber recovers nothing from a PDF with no text layer (older HKEXnews
filings, scanned government documents). When OCR is enabled, each page is
rendered to an image (PyMuPDF) and passed through Tesseract, which is multilingual
(``eng+kor+chi_tra`` by default), so Korean / English / Traditional-Chinese scans
all yield text.

OCR needs the Tesseract binary and PyMuPDF installed, so it is optional and off by
default; ``ocr_available`` reports whether the stack is present.
"""

from __future__ import annotations

import io

from .logging import get_logger

log = get_logger(__name__)


def ocr_available() -> bool:
    """True when PyMuPDF, Pillow, pytesseract, and the Tesseract binary are usable."""
    try:
        import fitz  # noqa: F401
        import pytesseract
        from PIL import Image  # noqa: F401

        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def ocr_pdf(content: bytes, languages: str = "eng", dpi: int = 300,
            max_pages: int = 50) -> str | None:
    """Render each PDF page and OCR it; return the concatenated text (or None)."""
    import fitz
    import pytesseract
    from PIL import Image

    parts: list[str] = []
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                log.info("OCR page cap (%d) reached; remaining pages skipped", max_pages)
                break
            try:
                pix = page.get_pixmap(dpi=dpi)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                parts.append(pytesseract.image_to_string(img, lang=languages))
            except Exception as exc:  # noqa: BLE001
                log.warning("OCR failed on page %d: %s", i + 1, exc)
    finally:
        doc.close()
    text = "\n".join(parts).strip()
    return text or None
