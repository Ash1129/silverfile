"""The Document Extraction agent, offline: payloads, fixture files and the
W-2 image read from its sidecar."""
from __future__ import annotations

import json

import pytest

from conftest import CASE1, CASE3, CASE4, DEMO, document
from silverfile.agents.extraction import SCHEMAS, classify_kind, extract_document_node, extract_one


def test_json_payload_is_extracted_as_typed_fields():
    rec = extract_one({"id": "int-p", "kind": "1099-INT", "payload": {"recipient_name": "Eleanor M Whitfield", "payer_name": "Finger Lakes Savings", "interest_income": "842.17", "federal_withheld": 0, "tax_exempt_interest": None, "tax_year": "2025"}})
    assert rec["id"] == "int-p" and rec["kind"] == "1099-INT"
    assert rec["method"] == "payload"
    assert rec["source_ref"] == "payload:int-p"
    assert rec["fields"]["interest_income"] == 842.17  # money fields become floats
    assert rec["fields"]["federal_withheld"] == 0.0
    assert rec["fields"]["tax_exempt_interest"] is None
    assert rec["boxes"] == SCHEMAS["1099-INT"]
    assert rec["issues"] == [] and rec["out_of_scope"] is False
    assert rec["version"] == 1 and rec["confidence"] == 1.0


def test_json_fixture_path_is_extracted():
    rec = extract_one(document("ssa-1", "SSA-1099", CASE1 / "ssa1099.json"))
    assert rec["method"] == "fixture"
    assert rec["kind"] == "SSA-1099"
    assert rec["fields"]["net_benefits"] == 21_600.0
    assert rec["fields"]["federal_withheld"] == 0.0
    assert rec["boxes"]["net_benefits"] == "Box 5"
    assert rec["source_ref"] == str(CASE1 / "ssa1099.json")
    assert rec["issues"] == []


def test_w2_image_is_read_offline_from_its_sidecar():
    rec = extract_one(document("w2-1", "W-2", DEMO / "w2_image_1.jpg"))
    assert rec["method"] == "offline-sidecar"
    assert rec["kind"] == "W-2"
    assert rec["fields"]["wages"] == 18_400.0
    assert rec["fields"]["federal_withheld"] == 1_210.0
    assert rec["fields"]["employer_name"] == "Tompkins Public Library"
    assert rec["fields"]["employee_ssn_last4"] == "5678"
    assert rec["boxes"]["wages"] == "Box 1"
    assert rec["boxes"]["federal_withheld"] == "Box 2"
    assert rec["out_of_scope"] is False
    assert any("sidecar" in i for i in rec["issues"])  # it says where the values came from
    assert rec["tokens_in"] == 0 and rec["tokens_out"] == 0


def test_second_w2_image_sidecar():
    rec = extract_one(document("w2-2", "W-2", DEMO / "w2_image_2.jpg"))
    assert rec["fields"]["wages"] == 6_250.0 and rec["fields"]["federal_withheld"] == 310.0
    assert rec["fields"]["employer_name"] == "Cornell Botanic Gardens"


def test_image_without_sidecar_is_marked_unreadable(tmp_path):
    img = tmp_path / "lonely.jpg"
    img.write_bytes(b"\xff\xd8\xff\xd9")
    rec = extract_one({"id": "img", "kind": "W-2", "path": str(img)})
    assert rec["confidence"] == 0.0
    assert any("could not be read" in i for i in rec["issues"])
    assert rec["fields"] == {}


def test_unknown_kind_is_out_of_scope():
    rec = extract_one({"id": "mystery", "kind": "unknown", "payload": {"something": 1}})
    assert rec["kind"].lower() == "unknown"  # declared kinds are upper-cased on the way in
    assert rec["out_of_scope"] is True
    assert rec["boxes"] == {}
    assert any("not in the SilverFile profile" in i for i in rec["issues"])


def test_1099_nec_fixture_is_out_of_scope_but_still_recorded():
    rec = extract_one(document("nec-1", "1099-NEC", CASE3 / "1099nec.json"))
    assert rec["kind"] == "1099-NEC" and rec["out_of_scope"] is True
    assert rec["fields"]["nonemployee_compensation"] == 4_300.0  # kept, so a person sees what was uploaded
    assert rec["fields"]["recipient_name"] == "Ruth Adler"


def test_1099r_box_2b_is_parsed_as_true():
    rec = extract_one(document("r-1", "1099-R", CASE4 / "1099r.json"))
    assert rec["kind"] == "1099-R"
    assert rec["fields"]["taxable_not_determined"] is True
    assert rec["fields"]["taxable_amount"] is None
    assert rec["fields"]["gross_distribution"] == 14_000.0
    assert rec["boxes"]["taxable_not_determined"] == "Box 2b"
    assert rec["out_of_scope"] is False  # the escalation is eligibility's call, not extraction's


@pytest.mark.parametrize("marker, expected", [("X", True), ("x", True), ("true", True), (1, True), (False, False), (None, False), ("", False)])
def test_1099r_checkbox_markers(marker, expected):
    rec = extract_one({"id": "r", "kind": "1099-R", "payload": {"gross_distribution": 100, "taxable_amount": 100, "taxable_not_determined": marker, "tax_year": "2025"}})
    assert rec["fields"]["taxable_not_determined"] is expected


def test_clean_1099r_fixture_has_box_2b_false():
    rec = extract_one(document("r-1", "1099-R", CASE1 / "1099r.json"))
    assert rec["fields"]["taxable_not_determined"] is False
    assert rec["fields"]["ira_sep_simple"] is False
    assert rec["fields"]["taxable_amount"] == 14_200.0


def test_non_2025_tax_year_produces_an_issue():
    rec = extract_one({"id": "old", "kind": "W-2", "payload": {"wages": 1000, "federal_withheld": 10, "tax_year": "2024"}})
    assert any("2024" in i and "not 2025" in i for i in rec["issues"])
    assert rec["out_of_scope"] is False


def test_unreadable_amount_and_missing_wages_are_issues():
    rec = extract_one({"id": "bad", "kind": "W-2", "payload": {"wages": "eighteen thousand", "tax_year": "2025"}})
    assert rec["fields"]["wages"] is None
    assert any("not a readable amount" in i for i in rec["issues"])
    assert any("Box 1 wages missing" in i for i in rec["issues"])


@pytest.mark.parametrize("given, expected", [("W2", "W-2"), ("w-2", "W-2"), ("Form W-2", "W-2"), ("ssa1099", "SSA-1099"), ("1099r", "1099-R"), ("1099-INT", "1099-INT"), ("1099nec", "1099-NEC"), ("", "unknown")])
def test_classify_kind_aliases(given, expected):
    assert classify_kind({"kind": given}, None) == expected


def test_kind_falls_back_to_the_payload_when_not_declared():
    rec = extract_one({"id": "p", "payload": {"kind": "SSA-1099", "net_benefits": 100, "tax_year": "2025"}})
    assert rec["kind"] == "SSA-1099"


def test_unsupported_input_is_reported():
    rec = extract_one({"id": "none", "kind": "W-2"})
    assert rec["confidence"] == 0.0
    assert any("unsupported document input" in i for i in rec["issues"])


def test_extract_document_node_writes_only_its_own_record():
    out = extract_document_node({"document": {**document("ssa-1", "SSA-1099", CASE1 / "ssa1099.json"), "version": 2}})
    assert list(out["extracted"]) == ["ssa-1"]
    assert out["extracted"]["ssa-1"]["version"] == 2
    assert out["token_log"] == []  # nothing was spent offline
    assert out["events"] == ["extraction: ssa-1 -> SSA-1099 via fixture (0 issues)"]


def test_sidecar_matches_the_image_fixture_definition():
    sidecar = json.loads((DEMO / "w2_image_1.json").read_text())
    assert sidecar["kind"] == "W-2" and sidecar["wages"] == 18_400.0 and sidecar["tax_year"] == "2025"
