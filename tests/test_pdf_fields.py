"""Tests for schema-driven field extraction and its provenance."""

from issuer_data.pdf_extract import StitchedTable
from issuer_data.pdf_fields import DEFAULT_SPECS, FieldSpec, extract_fields, load_specs
from issuer_data.pdf_validate import validate


def _table(rows, page=1):
    return StitchedTable(rows=rows, page_start=page, page_end=page)


def test_reads_key_value_tables_with_the_page_it_came_from():
    table = _table([["발행회사", "주식회사 테스트"],
                    ["증권의 종류", "무보증사채"],
                    ["발행금액", "100,000,000"]], page=7)
    found = extract_fields("", [table], DEFAULT_SPECS)
    assert found["issuer"].value == "주식회사 테스트"
    assert found["security_type"].value == "무보증사채"     # matched despite the space
    assert found["amount"].page == 7
    assert found["amount"].source == "table"
    assert "발행금액" in found["amount"].evidence


def test_reads_label_colon_value_from_the_narrative_with_a_page():
    page_text = {1: "표지", 4: "발행회사 : 주식회사 텍스트\n만기일 : 2027-05-01"}
    found = extract_fields("", [], DEFAULT_SPECS, page_text=page_text)
    assert found["issuer"].value == "주식회사 텍스트"
    assert found["maturity"].page == 4
    assert found["maturity"].source == "text"


def test_a_value_has_to_look_like_its_kind():
    """A number field must hold a number — otherwise a label two cells over gets
    read as the amount and nobody notices."""
    specs = [FieldSpec("amount", ["발행금액"], kind="number")]
    assert extract_fields("", [_table([["발행금액", "미정"]])], specs) == {}
    assert extract_fields("", [_table([["발행금액", "미정", "5,000"]])], specs)["amount"].value \
        == "5,000"


def test_labels_match_whole_cells_only():
    """'이자율' is a substring of '연체이자율'; a substring match reads the
    penalty rate as the coupon."""
    specs = [FieldSpec("coupon", ["이자율"], kind="number")]
    found = extract_fields("", [_table([["연체이자율", "12.0"], ["이자율", "3.5"]])], specs)
    assert found["coupon"].value == "3.5"


def test_a_missing_required_field_fails_the_document():
    pages = [{"page_no": 1, "width": 600.0, "height": 800.0, "tables": [],
              "lines": [{"text": "발행회사 : 주식회사 테스트", "x0": 50.0, "x1": 400.0,
                         "top": 300.0, "bottom": 315.0}]}]
    specs = [FieldSpec("issuer", ["발행회사"], required=True),
             FieldSpec("maturity", ["만기일"], kind="date", required=True)]
    report = validate(pages, "발행회사 : 주식회사 테스트", [], field_specs=specs,
                      )
    assert report.verdict == "fail"
    assert report.missing_fields == ["maturity"]


def test_no_schema_means_no_field_requirement():
    """Without a schema the gate rests on coverage and arithmetic — it must not
    fail every document for lacking fields nobody asked for."""
    pages = [{"page_no": 1, "width": 600.0, "height": 800.0, "tables": [],
              "lines": [{"text": "just some prose", "x0": 50.0, "x1": 400.0,
                         "top": 300.0, "bottom": 315.0}]}]
    assert validate(pages, "just some prose", []).verdict == "pass"


def test_specs_load_from_json(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text('[{"name": "isin", "labels": ["ISIN"], "required": true}]',
                    encoding="utf-8")
    specs = load_specs(path)
    assert specs[0].name == "isin" and specs[0].required
