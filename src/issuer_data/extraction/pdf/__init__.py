"""PDF extraction: layout analysis, table detection, and OCR fallback.

`extract` is the entry point and orchestrates the rest: `columns` splits
multi-column pages, `ml_tables` and `escalate` handle tables the cheap ruled
detector is not confident about, `ocr` covers image-only pages, and `agreement`
scores two engines' tables against each other.
"""
