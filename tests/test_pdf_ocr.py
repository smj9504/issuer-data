"""OCR fallback tests — gated on the Tesseract/PyMuPDF stack being installed."""

import glob
import io
import os
import sys
from functools import cache

import pytest

from issuer_data.extraction.pdf.ocr import ocr_available, ocr_pdf

# Rendering the test images needs a scalable font — any of them. Naming specific
# files skipped these tests everywhere the names did not match: two DejaVu paths
# meant every Windows machine reported "no scalable font available" while sitting
# on a directory full of them, so the OCR fallback went untested there.
_FONT_DIRS = {
    "win32": (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts")),
    "darwin": ("/System/Library/Fonts", "/Library/Fonts",
               os.path.expanduser("~/Library/Fonts")),
}
_FONT_DIRS_DEFAULT = ("/usr/share/fonts", "/usr/local/share/fonts",
                      os.path.expanduser("~/.fonts"))


@cache
def _font_path() -> str | None:
    """Any scalable font on this machine, or None if it genuinely has none."""
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "arial.ttf", "LiberationSans-Regular.ttf"):
        try:  # Pillow resolves bare names against the platform's own font dirs.
            return ImageFont.truetype(name, 12).path
        except OSError:
            continue
    for directory in _FONT_DIRS.get(sys.platform, _FONT_DIRS_DEFAULT):
        for ext in ("ttf", "otf", "ttc"):
            for path in sorted(glob.glob(os.path.join(directory, "**", f"*.{ext}"),
                                         recursive=True)):
                try:
                    ImageFont.truetype(path, 12)
                    return path
                except OSError:
                    continue  # bitmap-only or broken face; keep looking
    return None


def _font(size: int):
    """`_font_path` at `size`, skipping only when the machine really has no font."""
    from PIL import ImageFont

    path = _font_path()
    if path is None:
        pytest.skip("no scalable font available to render a legible test image")
    return ImageFont.truetype(path, size)


def _image_only_pdf(text: str) -> bytes:
    """A one-page PDF whose only content is a rendered image of `text` (no text layer)."""
    fitz = pytest.importorskip("fitz")
    from PIL import Image, ImageDraw

    font = _font(60)
    img = Image.new("RGB", (1400, 300), "white")
    ImageDraw.Draw(img).text((40, 110), text, fill="black", font=font)
    png = io.BytesIO()
    img.save(png, format="PNG")

    doc = fitz.open()
    page = doc.new_page(width=1400, height=300)
    page.insert_image(fitz.Rect(0, 0, 1400, 300), stream=png.getvalue())
    out = doc.tobytes()
    doc.close()
    return out


def test_ocr_recovers_text_from_image_only_pdf():
    if not ocr_available():
        pytest.skip("Tesseract/PyMuPDF not installed")
    pdf = _image_only_pdf("HELLO OCR 12345")
    text = ocr_pdf(pdf, languages="eng", dpi=200)
    assert text is not None
    joined = text.replace(" ", "").upper()
    assert "HELLOOCR" in joined or "12345" in joined


def test_documents_ocr_fallback_when_enabled(conn, tmp_path, monkeypatch):
    if not ocr_available():
        pytest.skip("Tesseract/PyMuPDF not installed")
    from issuer_data import documents
    from issuer_data.config import Settings
    from issuer_data.models import Company, Filing, Security
    from issuer_data.storage.repository import Repository

    pdf = _image_only_pdf("SCANNED REPORT 2024")
    settings = Settings(docs_dir=tmp_path, ocr_enabled=True, ocr_languages="eng")
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="ACME", cik="1", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="ACME", source="edgar"), cid)
    repo.upsert_filings(cid, [Filing(symbol="ACME", market="US", filing_id="f1",
                                     url="http://x/scan.pdf", source="edgar")])
    repo.commit()

    class _Resp:
        content = pdf
        headers = {"Content-Type": "application/pdf"}

    monkeypatch.setattr(documents, "_client", lambda s: type("C", (), {
        "get": lambda self, url, headers=None: _Resp()})())

    documents.backfill_documents(conn, settings, ["US:ACME"], None, None)
    row = conn.execute("SELECT text_content, doc_format FROM filing_documents").fetchone()
    assert row["doc_format"] == "pdf"
    assert row["text_content"] and "2024" in row["text_content"]  # OCR filled it


def test_ocr_is_enabled_by_default():
    """An image-only PDF is otherwise stored with no text at all, so OCR is on."""
    from issuer_data.config import Settings

    assert Settings(_env_file=None).ocr_enabled is True


def test_ocr_runs_without_being_asked_for(conn, tmp_path, monkeypatch):
    if not ocr_available():
        pytest.skip("Tesseract/PyMuPDF not installed")
    from issuer_data import documents
    from issuer_data.config import Settings
    from issuer_data.models import Company, Filing, Security
    from issuer_data.storage.repository import Repository

    # No ocr_enabled=True here: the default has to carry it.
    settings = Settings(_env_file=None, docs_dir=tmp_path, ocr_languages="eng")
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="ACME", cik="1", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="ACME", source="edgar"), cid)
    repo.upsert_filings(cid, [Filing(symbol="ACME", market="US", filing_id="f1",
                                     url="http://x/scan.pdf", source="edgar")])
    repo.commit()

    pdf = _image_only_pdf("DEFAULT OCR 2026")

    class _Resp:
        content = pdf
        headers = {"Content-Type": "application/pdf"}

    monkeypatch.setattr(documents, "_client", lambda s: type("C", (), {
        "get": lambda self, url, headers=None: _Resp()})())
    documents.backfill_documents(conn, settings, ["US:ACME"], None, None)

    row = conn.execute("SELECT text_content FROM filing_documents").fetchone()
    assert row["text_content"] and "2026" in row["text_content"]


def test_missing_engine_warns_once_with_install_instructions(caplog, monkeypatch):
    """Enabled-by-default OCR that cannot run must say so — once, not per page."""
    import logging

    from issuer_data.extraction.pdf import ocr as pdf_ocr

    pdf_ocr.ocr_ready.cache_clear()
    monkeypatch.setattr(pdf_ocr, "ocr_available", lambda: False)
    with caplog.at_level(logging.WARNING):
        assert pdf_ocr.ocr_ready() is False
        assert pdf_ocr.ocr_ready() is False  # cached: still one warning
    pdf_ocr.ocr_ready.cache_clear()

    warnings = [r for r in caplog.records if "OCR is enabled but unavailable" in r.message]
    assert len(warnings) == 1
    assert "apt install tesseract-ocr" in caplog.text
    assert "--no-ocr" in caplog.text


def _mixed_pdf(prose_before: str, image_text: list[str], prose_after: str) -> bytes:
    """Text page, an image-only page carrying `image_text`, then another text page."""
    fitz = pytest.importorskip("fitz")
    from PIL import Image, ImageDraw

    font = _font(34)
    img = Image.new("RGB", (1000, 160 + 90 * len(image_text)), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(image_text):
        draw.text((40, 60 + i * 90), line, fill="black", font=font)
    png = io.BytesIO()
    img.save(png, format="PNG")

    doc = fitz.open()
    doc.new_page(width=560, height=400).insert_text((50, 80), prose_before, fontsize=11)
    doc.new_page(width=560, height=300).insert_image(
        fitz.Rect(0, 0, 560, 280), stream=png.getvalue())
    doc.new_page(width=560, height=400).insert_text((50, 80), prose_after, fontsize=11)
    out = doc.tobytes()
    doc.close()
    return out


def test_a_chart_page_inside_a_text_pdf_is_not_silently_dropped():
    """OCR used to need the *whole* document to be empty, so a chart page vanished
    while the surrounding prose still referred to "the chart above"."""
    if not ocr_available():
        pytest.skip("Tesseract/PyMuPDF not installed")
    from issuer_data import documents
    from issuer_data.config import Settings

    pdf = _mixed_pdf("The Group reports segment revenues as set out below.",
                     ["VAS 96110", "Marketing 38171"],
                     "Further commentary follows the chart above.")
    plain = documents.extract_text(pdf, "pdf") or ""
    assert "96110" not in plain          # the text layer really is missing it

    settings = Settings(_env_file=None, ocr_languages="eng")
    filled = documents._ocr_pages_without_text(pdf, plain, settings, "http://x/mixed.pdf")
    assert "96110" in filled and "38171" in filled
    # spliced back in order, between the pages that do have text
    assert filled.index("segment revenues") < filled.index("96110") < filled.index("commentary")


def test_pages_with_text_are_not_re_ocred():
    if not ocr_available():
        pytest.skip("Tesseract/PyMuPDF not installed")
    from issuer_data.extraction.pdf.ocr import ocr_pages

    pdf = _mixed_pdf("Page one prose.", ["ONLY IMAGE 4242"], "Page three prose.")
    recovered = ocr_pages(pdf, languages="eng", dpi=150, only={1})
    assert set(recovered) == {1}
    assert "4242" in recovered[1].replace(" ", "")


def test_text_less_pages_are_reported_when_ocr_is_unavailable(caplog, monkeypatch):
    """Silence is the failure mode: say which pages have nothing rather than
    storing a document that quietly lost some."""
    import logging

    from issuer_data import documents
    from issuer_data.config import Settings

    pdf = _mixed_pdf("Before.", ["HIDDEN 777"], "After.")
    monkeypatch.setattr("issuer_data.extraction.pdf.ocr.ocr_ready", lambda: False)
    with caplog.at_level(logging.WARNING):
        documents._ocr_pages_without_text(pdf, "Before.\nAfter.",
                                          Settings(_env_file=None), "http://x/m.pdf")
    assert "no text layer" in caplog.text
