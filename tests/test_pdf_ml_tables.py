"""The optional ML table tier: Table Transformer and Docling.

These models are MIT-licensed and run locally, so the only cost is weight — a
few GB of torch and model files. The tests that need them skip when they are
absent; everything else here runs without the extra installed, because the parts
that matter when it is *not* installed are exactly the parts that must not break
a normal run.
"""

import logging

import pytest
from test_pdf_columns import _financial_pdf  # same directory, shared PDF fixture

from issuer_data import pdf_extract
from issuer_data.cli import build_parser
from issuer_data.config import Settings
from issuer_data.pdf_ml_tables import _nearest_column, ml_available, ml_ready


@pytest.fixture(autouse=True)
def _reset_warning_cache():
    ml_ready.cache_clear()
    yield
    ml_ready.cache_clear()


# ------------------------------------------------------------------- defaults
def test_ml_tier_is_off_by_default():
    """Several GB of torch is not something a default install should assume."""
    settings = Settings(_env_file=None)
    assert settings.pdf_ml_tables is False
    assert settings.pdf_ml_engine == "table-transformer"


def test_unknown_engine_is_reported_not_guessed(caplog):
    with caplog.at_level(logging.WARNING):
        assert ml_ready("tesseract-tables") is False
    assert "unknown ML table engine" in caplog.text


def test_missing_engine_warns_once_with_install_instructions(caplog, monkeypatch):
    monkeypatch.setattr("issuer_data.pdf_ml_tables.ml_available", lambda engine: False)
    with caplog.at_level(logging.WARNING):
        assert ml_ready("table-transformer") is False
        assert ml_ready("table-transformer") is False       # cached
    warnings = [r for r in caplog.records if "not installed" in r.message]
    assert len(warnings) == 1
    assert "pip install '.[ml]'" in caplog.text
    assert "--ml-tables" in caplog.text


def test_requesting_an_uninstalled_engine_falls_back_not_fails(monkeypatch, caplog):
    """A missing extra must degrade to the built-in detectors, not lose tables."""
    monkeypatch.setattr("issuer_data.pdf_ml_tables.ml_available", lambda engine: False)
    with caplog.at_level(logging.WARNING):
        doc = pdf_extract.extract_structured(_financial_pdf(), ml_engine="table-transformer")
    assert [t.source_engine for t in doc.tables] == ["column-geometry"]


# ------------------------------------------------------------------------ CLI
@pytest.mark.parametrize("command", ["collect", "download-docs"])
def test_ml_engine_flag_implies_the_tier(command):
    argv = [command, "--market", "hk", "--type", "filings"] if command == "collect" else [command]
    args = build_parser().parse_args([*argv, "--ml-engine", "docling"])
    settings = Settings(_env_file=None)
    if getattr(args, "ml_tables", False):
        settings.pdf_ml_tables = True
    if getattr(args, "ml_engine", None):
        settings.pdf_ml_engine = args.ml_engine
        settings.pdf_ml_tables = True
    assert settings.pdf_ml_tables is True
    assert settings.pdf_ml_engine == "docling"


def test_no_ml_flag_leaves_the_tier_off():
    args = build_parser().parse_args(["collect", "--market", "hk", "--type", "filings"])
    assert getattr(args, "ml_tables", False) is False
    assert getattr(args, "ml_engine", None) is None


# ------------------------------------------------------ cell assignment (pure)
def test_word_inside_a_column_lands_in_it():
    spans = [(0.0, 100.0), (120.0, 200.0), (220.0, 300.0)]
    assert _nearest_column(50.0, spans) == 0
    assert _nearest_column(150.0, spans) == 1
    assert _nearest_column(250.0, spans) == 2


def test_word_outside_every_column_goes_to_the_nearest():
    """Predicted boxes sit inside the ink, so strict containment drops real text."""
    spans = [(0.0, 100.0), (120.0, 200.0), (220.0, 300.0)]
    assert _nearest_column(-30.0, spans) == 0     # left of the label column
    assert _nearest_column(114.0, spans) == 1     # in the gutter, closer to col 1
    assert _nearest_column(400.0, spans) == 2     # right of the last column


# -------------------------------------------------- real models, when present
requires_tatr = pytest.mark.skipif(not ml_available("table-transformer"),
                                   reason="ML extra not installed")
requires_docling = pytest.mark.skipif(not ml_available("docling"),
                                      reason="ML extra not installed")


@requires_tatr
def test_table_transformer_reads_a_financial_table():
    doc = pdf_extract.extract_structured(_financial_pdf(), ml_engine="table-transformer")
    assert doc.tables
    assert doc.tables[0].source_engine == "table-transformer"
    flat = [cell for row in doc.tables[0].rows for cell in row]
    assert "196,458" in flat and "180,022" in flat


@requires_tatr
def test_table_transformer_keeps_whole_row_labels():
    """The first word of a label used to be dropped as "outside" the column."""
    doc = pdf_extract.extract_structured(_financial_pdf(), ml_engine="table-transformer")
    labels = " ".join(row[0] for row in doc.tables[0].rows)
    assert "Gross profit" in labels


@requires_docling
def test_docling_tables_are_merged_by_page():
    doc = pdf_extract.extract_structured(_financial_pdf(), ml_engine="docling")
    assert {t.source_engine for t in doc.tables} <= {"docling"}


def test_docling_failure_falls_back_to_the_built_ins(monkeypatch, caplog):
    def _boom(content):
        raise RuntimeError("model download failed")

    monkeypatch.setattr("issuer_data.pdf_ml_tables.ml_available", lambda engine: True)
    monkeypatch.setattr("issuer_data.pdf_ml_tables.find_docling_tables", _boom)
    with caplog.at_level(logging.WARNING):
        doc = pdf_extract.extract_structured(_financial_pdf(), ml_engine="docling")
    assert "docling conversion failed" in caplog.text
    assert [t.source_engine for t in doc.tables] == ["column-geometry"]
