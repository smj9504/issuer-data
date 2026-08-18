"""OCR fallback tests — gated on the Tesseract/PyMuPDF stack being installed."""

import io

import pytest

from issuer_data.pdf_ocr import ocr_available, ocr_pdf


def _image_only_pdf(text: str) -> bytes:
    """A one-page PDF whose only content is a rendered image of `text` (no text layer)."""
    fitz = pytest.importorskip("fitz")
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(path, 60)
            break
        except OSError:
            continue
    if font is None:
        pytest.skip("no scalable font available to render a legible test image")

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
