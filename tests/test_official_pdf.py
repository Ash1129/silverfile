"""Official field mapping, logical/widget consistency and provenance navigation."""
from __future__ import annotations

import copy
import io
import logging

import pytest
from pypdf import PdfReader

from conftest import CASE1, CASE2, case1_document_list, case2_document_list, load_intake, resolve, run_prefilled
from silverfile.forms.official import ALIASES, FILING_FIELDS, LINE_FIELDS, TEMPLATE, field, field_values, _full_name
from silverfile.forms.render import render_html, render_pdf


@pytest.fixture(scope="module")
def official_cases():
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    result = {}
    for name, documents, folder in (("single", case1_document_list(), CASE1), ("joint", case2_document_list(), CASE2)):
        graph, cfg, state = run_prefilled(f"official-test-{name}", documents, load_intake(folder))
        if name == "joint":
            state = resolve(graph, cfg, "continue", facts_patch={"filing_status": "mfj"})
        assert state["status"] == "complete"
        before = copy.deepcopy(state)
        data = render_pdf(state)
        assert state == before
        result[name] = state, PdfReader(io.BytesIO(data))
    return result


def test_official_source_pages_and_actual_money_boxes(official_cases):
    _, single = official_cases["single"]
    _, joint = official_cases["joint"]
    assert len(single.pages) > 4 and "Standard Deduction Chart" in single.pages[3].extract_text()
    assert "Appendix A" in single.pages[4].extract_text()
    values = single.get_fields()
    assert values[field(1, "f1_47[0]")]["/V"] == "18,400.00"  # printed line 1a
    assert values[field(1, "f1_57[0]")]["/V"] == "18,400.00"  # printed line 1z
    assert values[field(2, "f2_12[0]")]["/V"] == "13,205.84"  # printed line 6b
    assert values[field(2, "f2_18[0]")]["/V"] == values[field(2, "f2_19[0]")]["/V"]
    values = joint.get_fields()
    assert values[field(2, "f2_20[0]")]["/V"] == "36,300.00"
    assert values[field(2, "f2_22[0]")]["/V"] == "12,000.00"
    assert values[field(3, "f3_14[0]")]["/V"] == "4,000.00"
    assert values[field(4, "f4_01[0]")]["/V"] == "3"


@pytest.mark.parametrize("case", ["single", "joint"])
def test_interactive_tree_widgets_and_appearances_agree(official_cases, case):
    state, reader = official_cases[case]
    canonical = reader.get_fields()
    assert len(canonical) == len(PdfReader(TEMPLATE).get_fields())
    assert "/XFA" not in reader.trailer["/Root"]["/AcroForm"]
    expected = field_values(state)
    seen = set()
    for page in reader.pages[:4]:
        assert "SILVERFILE DRAFT - NOT FILED" in page.extract_text()
        for ref in page.get("/Annots", []):
            widget = ref.get_object()
            if widget.get("/Subtype") != "/Widget":
                continue
            name = _full_name(widget)
            assert name not in seen and name in canonical
            seen.add(name)
            if name in expected:
                assert str(canonical[name]["/V"]) == expected[name]
                assert str(widget.get("/V")) == expected[name]
                appearance = widget["/AP"]["/N"].get_object()
                if widget.get("/FT") == "/Btn":
                    assert widget["/AS"] == expected[name]
                    assert appearance[expected[name]].get_object().get_data()
                else:
                    assert appearance.get_data()
    assert len(seen) == 200


def test_masked_identifiers_and_blank_uncollected_elections(official_cases):
    state, reader = official_cases["joint"]
    fields = reader.get_fields()
    assert fields[field(1, "YourSocial[0].f1_16[0]")]["/V"] == "XXXXX6789"
    assert fields[field(1, "f1_19[0]")]["/V"] == "XXXXX8901"
    assert fields[field(1, "f1_08[0]")]["/V"] == "05"
    assert fields[field(1, "f1_09[0]")]["/V"] == "09"
    assert fields[field(1, "f1_10[0]")]["/V"] == "2025"
    for name in ("Page1[0].Address[0].f1_20[0]", "Page3[0].f3_15[0]", "Page3[0].RoutingNo[0].f3_16[0]", "Page3[0].AccountNo[0].f3_17[0]", "Page3[0].f3_24[0]"):
        assert not fields["topmostSubform[0]." + name].get("/V")
    text = "\n".join(page.extract_text() for page in reader.pages)
    for person in (state["intake"]["taxpayer"], state["intake"]["spouse"]):
        assert person["ssn"] not in text and person["ssn"].replace("-", "") not in text


def test_amount_labels_link_to_real_provenance_pages(official_cases):
    _, reader = official_cases["single"]
    pages = {page.indirect_reference.idnum: index for index, page in enumerate(reader.pages)}
    links = [ref.get_object() for page in reader.pages[:4] for ref in page.get("/Annots", []) if ref.get_object().get("/Subtype") == "/Link"]
    assert len(links) == len(LINE_FIELDS) + len(ALIASES)
    for link in links:
        target = pages[link["/Dest"][0].idnum]
        assert target >= 4 and "Line " in reader.pages[target].extract_text()


@pytest.mark.parametrize("status", ["single", "mfj", "mfs", "hoh", "qss"])
def test_each_filing_status_has_one_explicit_choice(status):
    values = field_values({"intake": {"filing_status": status}})
    assert [(key, values[key]) for key, _ in FILING_FIELDS.values() if key in values] == [FILING_FIELDS[status]]


def test_pending_return_does_not_invent_zero_amounts_or_box_counts():
    state = {"return_id": "official-pending", "tax_year": 2025, "status": "waiting_human", "intake": load_intake(CASE2), "eligibility": {"filing_status_valid": False, "box_count": 0}}
    reader = PdfReader(io.BytesIO(render_pdf(state)))
    fields = reader.get_fields()
    assert not fields[LINE_FIELDS["1z"]].get("/V")
    assert not fields[LINE_FIELDS["12e"]].get("/V")
    assert not fields[field(4, "f4_01[0]")].get("/V")
    assert "Needs a person" in "\n".join(p.extract_text() for p in reader.pages)


def test_more_than_four_dependents_are_flagged_and_preserved(official_cases):
    state = copy.deepcopy(official_cases["single"][0])
    state["intake"]["dependents"] = [{"name": f"Example Dependent{index}", "relationship": "grandchild"} for index in range(5)]
    reader = PdfReader(io.BytesIO(render_pdf(state)))
    assert reader.get_fields()[field(1, "Dependents_ReadOrder[0].c1_11[0]")]["/V"] == "/1"
    assert "Dependent4" in "\n".join(p.extract_text() for p in reader.pages[4:])


def test_wrong_tax_year_or_replaced_template_is_refused(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="2025 only"):
        render_pdf({"tax_year": 2024})
    from silverfile.forms import official
    wrong = tmp_path / "wrong-template.pdf"
    wrong.write_bytes(b"wrong version")
    monkeypatch.setattr(official, "TEMPLATE", wrong)
    with pytest.raises(ValueError, match="verified 2025 field map"):
        render_pdf({"tax_year": 2025})


def test_html_keeps_clickable_provenance_and_points_to_official_pdf(official_cases):
    page = render_html(official_cases["single"][0], dashboard_url="/returns/example")
    assert "Official 1040-SR draft PDF" in page and "prov-card" in page
