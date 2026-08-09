import io
import zipfile

from issuer_data.documents import _format_from, extract_text


def test_extract_html_text():
    html = b"<html><body><h1>Title</h1><p>Hello <b>world</b></p><script>x=1</script></body></html>"
    text = extract_text(html, "html")
    assert "Hello" in text and "world" in text
    assert "x=1" not in text  # script stripped


def test_extract_txt():
    assert extract_text(b"plain text body", "txt") == "plain text body"


def test_extract_zip_of_xml():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("report.xml", "<doc><title>사업보고서</title><body>내용</body></doc>")
    text = extract_text(buf.getvalue(), "zip")
    assert "사업보고서" in text


def test_format_detection():
    assert _format_from("https://x/y.pdf", None) == "pdf"
    assert _format_from("https://x/y", "application/pdf") == "pdf"
    assert _format_from("https://sec.gov/a.htm", None) == "html"
    assert _format_from("https://x/a.zip", None) == "zip"
    assert _format_from("https://x/a.xml", None) == "xml"
