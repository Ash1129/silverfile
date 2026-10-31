"""Evidence conflicts, authenticated transcription repair, and safe recomputation."""
from __future__ import annotations

import copy
import html
import json

import pytest

from silverfile import graph, service
from silverfile.agents.computation import run_computation
from silverfile.agents.extraction import extract_one
from silverfile.agents.review import rule_checks
from silverfile.reconciliation import clean_claims, evidence_conflicts
from silverfile.security import audit, auth

SYSTEM = auth.system("reconciliation-test")
PREPARER = auth.Principal(kind="staff", subject="evidence-reviewer", role="preparer")
CLERK = auth.Principal(kind="staff", subject="clerk", role="clerk")


def start_conflict(*, duplicate=False, fault=None):
    documents = service.fixture_documents("case1_clean")
    w2 = next(d for d in documents if d["kind"] == "W-2")
    facts = service.fixture_intake("case1_clean")
    if duplicate:
        documents.append({**w2, "id": "duplicate-w2"})
    else:
        facts["document_claims"] = [{"document_id": w2["id"], "kind": "W-2", "field": "wages", "value": 35000}]
    rid = service.create_return(documents, facts, fault, actor=SYSTEM)
    state = service.run(rid)
    assert state["status"] == "waiting_human" and not state["lines"]
    return rid, w2["id"], state


def correction(state, key, **extra):
    return {"document_id": key, "expected_version": state["extracted"][key]["version"], "fields": {"wages": 35000},
            "evidence": "Preparer checked Box 1 against the original synthetic W-2 image and confirmed the transcription correction.", **extra}


def test_conflict_has_both_values_boxes_and_a_required_action(data_dir):
    rid, key, state = start_conflict()
    conflict = evidence_conflicts(state)[0]
    assert conflict["code"] == "amount_conflict"
    assert conflict["sources"][0]["value"] == 35000
    assert conflict["sources"][1]["box"] == "Box 1"
    assert conflict["sources"][1]["document_id"] == key
    assert service.pending(rid)["escalation"]["details"]["required_actions"]
    out = service.resolve(rid, {"action": "continue"}, actor=PREPARER)
    assert out["status"] == "waiting_human" and out["hitl_log"][-1]["result"] == "no"


def test_document_correction_recomputes_once_and_keeps_source_and_audit(data_dir):
    rid, key, state = start_conflict()
    original = copy.deepcopy(state["documents_in"])
    other_versions = {k: d["version"] for k, d in state["extracted"].items() if k != key}
    out = service.resolve(rid, {"action": "correct_document", "document_patch": correction(state, key)}, actor=PREPARER)
    assert out["status"] == "complete" and out["lines"]["1z"] == 35000
    assert out["computation_version"] == 1
    assert out["documents_in"] == original
    assert out["extracted"][key]["version"] > state["extracted"][key]["version"]
    assert other_versions == {k: d["version"] for k, d in out["extracted"].items() if k != key}
    assert out["extracted"][key]["correction"]["actor"] == PREPARER.subject
    assert out["provenance"]["1z"]["inputs"]["document_versions"][key] == out["extracted"][key]["version"]
    assert not evidence_conflicts(out)
    assert any(entry["event"] == "document_correction_requested" and entry["old_values"]["wages"] != entry["new_values"]["wages"] for entry in out["hitl_log"])
    assert any(entry["kind"] == "hitl_workflow" and entry["detail"].get("event") == "document_correction_applied" for entry in audit.recent(100, return_id=rid))
    graph._compiled.clear()
    assert service.get_state(rid)["document_overrides"][key]["actor"] == PREPARER.subject


def test_correcting_the_statement_also_requires_evidence_and_rechecks(data_dir):
    rid, key, state = start_conflict()
    claim = {"document_id": key, "kind": "W-2", "field": "wages", "value": state["extracted"][key]["fields"]["wages"]}
    out = service.resolve(rid, {"action": "continue", "facts_patch": {"document_claims": [claim]}}, actor=PREPARER)
    assert out["status"] == "waiting_human"
    assert out["intake"]["document_claims"][0]["value"] == 35000
    out = service.resolve(rid, {"action": "continue", "note": "Checked the image and confirmed the original statement was mistaken.", "facts_patch": {"document_claims": [claim]}}, actor=PREPARER)
    assert out["status"] == "complete"
    assert any(e["event"] == "document_claims_corrected" for e in out["hitl_log"])


@pytest.mark.parametrize("change", [
    {"expected_version": 0}, {"document_id": "not-on-this-return"}, {"evidence": " "},
    {"fields": {"wages": "unreadable"}}, {"fields": {"wages": -1}}, {"fields": {"wages": None}},
    {"fields": {"status": "approved"}}, {"fields": []}, {"superseded_by": "another-return"},
])
def test_bad_document_corrections_leave_checkpoint_and_interrupt_unchanged(data_dir, change):
    rid, key, state = start_conflict()
    before = service.state_json(rid)
    pending = service.pending_id(rid)
    with pytest.raises(service.FactsPatchError):
        service.resolve(rid, {"action": "correct_document", "document_patch": correction(state, key, **change)}, actor=PREPARER)
    assert service.state_json(rid) == before and service.pending_id(rid) == pending


def test_duplicate_source_needs_a_disposition_and_is_not_counted_twice(data_dir):
    rid, key, state = start_conflict(duplicate=True)
    assert evidence_conflicts(state)[0]["code"] == "duplicate_source"
    out = service.resolve(rid, {"action": "correct_document", "document_patch": correction(state, "duplicate-w2", fields={}, superseded_by=key)}, actor=PREPARER)
    assert out["status"] == "complete"
    assert out["lines"]["1z"] == state["extracted"][key]["fields"]["wages"]
    assert "duplicate-w2" in out["extracted"] and out["extracted"]["duplicate-w2"]["superseded_by"] == key
    assert out["provenance"]["1z"]["sources"] == [key]


def test_different_w2s_are_not_duplicates_or_ambiguous_totals():
    from conftest import case1_document_list, DEMO
    docs = case1_document_list()
    docs.append({"id": "second-w2", "kind": "W-2", "path": str(DEMO / "w2_image_2.jpg")})
    state = {"extraction_done": True, "documents_in": docs, "extracted": {d["id"]: extract_one(d) for d in docs}}
    assert evidence_conflicts(state) == []
    state["intake"] = {"document_claims": [{"kind": "W-2", "field": "wages", "value": 35000}]}
    assert evidence_conflicts(state)[0]["code"] == "claim_source"


def test_stale_eligibility_cannot_bypass_evidence_gate(data_dir):
    _, _, state = start_conflict()
    assert run_computation(state).get("escalations")
    assert graph.finalize_node(state)["status"] == "halted"


def test_document_correction_does_not_skip_the_blocking_review_acceptance_gate(data_dir):
    facts = service.fixture_intake("case1_clean")
    docs = service.fixture_documents("case1_clean")
    key = next(d["id"] for d in docs if d["kind"] == "W-2")
    rid = service.create_return(docs, facts, {"drop_provenance_for": "6b", "rounds": 3}, actor=SYSTEM)
    state = service.run(rid)
    assert state["blocking_review"]["status"] == "open"
    version = state["computation_version"]
    out = service.resolve(rid, {"action": "correct_document", "document_patch": correction(state, key)}, actor=PREPARER)
    assert out["lines"]["1z"] == 35000 and out["computation_version"] == version + 1
    assert out["status"] == "waiting_human" and out["blocking_review"]["status"] == "open"
    out = service.resolve(rid, {"action": "accept"}, actor=PREPARER)
    assert out["status"] == "waiting_human" and out["hitl_log"][-1]["result"] == "no"


def test_explicit_w2_statement_is_captured_without_new_intake_questions(data_dir):
    facts = service.fixture_intake("case1_clean")
    facts["_confirmed"] = False
    rid = service.create_return(service.fixture_documents("case1_clean"), facts, actor=SYSTEM)
    service.run(rid)
    state = service.answer(rid, "Yes. My W-2 wages were $35,000.", actor=auth.taxpayer_principal(rid))
    assert state["status"] == "waiting_human"
    assert evidence_conflicts(state)[0]["code"] == "amount_conflict"


def test_document_correction_cannot_clear_a_security_finding(data_dir):
    from silverfile.reconciliation import clean_document_patch
    _, key, state = start_conflict()
    state["extracted"][key]["security_flags"] = ["Suspicious source instructions"]
    with pytest.raises(ValueError, match="security-flagged"):
        clean_document_patch(correction(state, key), state)


def test_structurally_identical_payloads_are_not_proof_of_duplicate_sources():
    docs = [{"id": key, "kind": "W-2", "payload": {"wages": 1000}} for key in ("first", "second")]
    state = {"documents_in": docs, "extraction_done": True, "extracted": {d["id"]: extract_one(d) for d in docs}}
    assert evidence_conflicts(state) == []


def test_unfinished_facts_edit_cannot_be_lost_to_a_document_correction(data_dir):
    rid, key, state = start_conflict()
    service.resolve(rid, {"action": "save_edit", "facts_patch": {"estimated_payments": 1000}}, actor=PREPARER)
    before = service.state_json(rid)
    with pytest.raises(service.FactsPatchError, match="Complete the current facts edit"):
        service.resolve(rid, {"action": "correct_document", "document_patch": correction(state, key)}, actor=PREPARER)
    assert service.state_json(rid) == before


def test_correcting_a_taxable_amount_does_not_override_a_true_box2b(data_dir):
    payload = json.loads((service.FIXTURES_DIR / "case4_r_box2b" / "1099r.json").read_text())
    facts = {**service.fixture_intake("case1_clean"), "documents_declared": ["1099-R"]}
    rid = service.create_return([{"id": "r-source", "kind": "1099-R", "payload": payload}], facts, actor=SYSTEM)
    state = service.run(rid)
    out = service.resolve(rid, {"action": "correct_document", "document_patch": correction(state, "r-source", fields={"taxable_amount": 1000})}, actor=PREPARER)
    assert out["status"] == "waiting_human" and not out["lines"]
    assert out["extracted"]["r-source"]["fields"]["taxable_not_determined"] is True
    assert out["hitl_log"][-1]["result"] == "no"


@pytest.mark.parametrize("role", ["clerk", "taxpayer"])
def test_document_correction_and_source_access_are_staff_only(data_dir, role):
    rid, key, state = start_conflict()
    actor = CLERK if role == "clerk" else auth.taxpayer_principal(rid)
    with pytest.raises(service.Forbidden):
        service.resolve(rid, {"action": "correct_document", "document_patch": correction(state, key)}, actor=actor)
    with pytest.raises(service.Forbidden):
        service.original_document(rid, key, actor=actor)


def test_correction_form_preserves_bad_input_and_can_retry(web):
    from webhelpers import interrupt_id, staff_client, token
    rid, key, state = start_conflict()
    client, _ = staff_client(web, "source-reviewer")
    page = client.get(f"/returns/{rid}")
    assert "Conflicting evidence" in page.text and "Box 1" in page.text
    source = client.get(f"/returns/{rid}/documents/{key}/source")
    assert source.status_code == 200 and source.headers["content-disposition"].startswith("attachment")
    values = {"document_id": key, "expected_version": str(state["extracted"][key]["version"]), "fields_json": '{"wages": }',
              "evidence": "Evidence note <script>bad()</script>", "csrf_token": token(page.text), "interrupt_id": interrupt_id(page.text)}
    before = service.state_json(rid)
    bad = client.post(f"/returns/{rid}/document-correction", data=values, follow_redirects=False)
    assert bad.status_code == 400 and "Invalid document JSON at line" in bad.text
    assert '{"wages": }' in html.unescape(bad.text) and "&lt;script&gt;" in bad.text
    assert service.state_json(rid) == before
    values.update(fields_json='{"wages":35000}', csrf_token=token(bad.text), interrupt_id=interrupt_id(bad.text))
    good = client.post(f"/returns/{rid}/document-correction", data=values, follow_redirects=False)
    assert good.status_code == 303 and service.get_state(rid)["status"] == "complete"


def test_document_box_in_facts_form_points_to_the_separate_source_editor(web):
    from webhelpers import interrupt_id, staff_client, token
    rid, key, state = start_conflict()
    client, _ = staff_client(web, "wrong-editor-reviewer")
    page = client.get(f"/returns/{rid}")
    before = service.state_json(rid)
    wrong = client.post(f"/returns/{rid}/resolve", data={"action": "continue", "facts_patch": '{"wages":35000}',
                        "csrf_token": token(page.text), "interrupt_id": interrupt_id(page.text)}, follow_redirects=False)
    assert wrong.status_code == 400
    assert "is a document box value, not an intake fact" in wrong.text
    assert "Use Document transcription review" in wrong.text
    assert 'href="#document-transcription-review"' in wrong.text
    assert 'id="document-transcription-review"' in wrong.text
    assert service.state_json(rid) == before
    right = client.post(f"/returns/{rid}/document-correction", data={"document_id": key, "expected_version": state["extracted"][key]["version"],
                        "fields_json": '{"wages":35000}', "evidence": "Checked the original synthetic W-2 Box 1.",
                        "csrf_token": token(wrong.text), "interrupt_id": interrupt_id(wrong.text)}, follow_redirects=False)
    assert right.status_code == 303 and service.get_state(rid)["status"] == "complete"


@pytest.mark.parametrize("claim", [None, {}, [{"kind": [], "field": "wages", "value": 1}], [{"kind": "W-2", "field": "status", "value": 1}], [{"kind": "W-2", "field": "wages", "value": float("nan")}]] )
def test_claim_validation_rejects_ambiguous_or_untyped_inputs(claim):
    with pytest.raises(ValueError):
        clean_claims(claim)


@pytest.mark.parametrize("kind, expected", [
    ("W-2", {"wages", "federal_withheld"}),
    ("SSA-1099", {"net_benefits", "federal_withheld"}),
    ("1099-R", {"gross_distribution", "taxable_amount", "taxable_not_determined", "ira_sep_simple", "qcd_amount", "federal_withheld"}),
    ("1099-INT", {"interest_income", "tax_exempt_interest", "federal_withheld"}),
    ("1099-DIV", {"ordinary_dividends", "qualified_dividends", "federal_withheld"}),
])
def test_missing_document_keys_block_computation_before_tool_calls(kind, expected, monkeypatch):
    from silverfile.agents import computation
    def forbidden_tool():
        pytest.fail("Unknown document amounts must be escalated before tools run.")
    monkeypatch.setattr(computation, "TaxMathClient", forbidden_tool)
    out = run_computation({"intake": service.fixture_intake("case1_clean"), "extracted": {"unreadable": {"kind": kind, "fields": {}}}})
    assert "lines" not in out
    escalation = out["escalations"][0]
    assert escalation["kind"] == "uncertain_fact"
    assert {action["field"] for action in escalation["details"]["required_actions"]} == {f"document:unreadable:{key}" for key in expected}


def test_unreadable_w2_after_legacy_eligibility_can_be_confirmed_and_exported(web, monkeypatch):
    import io
    from pypdf import PdfReader
    from silverfile.agents import eligibility
    from silverfile.forms.official import field
    from webhelpers import interrupt_id, staff_client, token
    rid = service.new_return_id()
    upload = service.save_upload(rid, "w2.jpg", service.DEMO_W2_IMAGE.read_bytes(), actor=SYSTEM)
    facts = {**service.fixture_intake("case1_clean"), "documents_declared": ["W-2"]}
    service.create_return([upload], facts, return_id=rid, actor=SYSTEM)
    # Reproduce the user's saved pre-fix eligibility result. Computation must
    # still reject the empty record, then permit an audited source repair.
    with monkeypatch.context() as legacy:
        legacy.setattr(eligibility, "unknown_fact_actions", lambda *args, **kwargs: [])
        out = service.run(rid)
    assert out["status"] == "waiting_human" and not out["lines"]
    assert service.pending(rid)["escalation"]["id"] == "esc-computation-unknown"
    assert out["extracted"][upload["id"]]["fields"] == {}
    assert "offline mode and no sidecar" in out["extracted"][upload["id"]]["issues"][0]
    client, _ = staff_client(web, "unreadable-document-reviewer")
    page = client.get(f"/returns/{rid}")
    assert "Document transcription review" in page.text and "federal_withheld" in page.text
    assert client.get(f"/returns/{rid}/documents/{upload['id']}/source").status_code == 200
    response = client.post(f"/returns/{rid}/document-correction", data={
        "csrf_token": token(page.text), "interrupt_id": interrupt_id(page.text),
        "document_id": upload["id"], "expected_version": out["extracted"][upload["id"]]["version"],
        "fields_json": '{"wages":18400,"federal_withheld":1210}',
        "evidence": "Inspected the original synthetic W-2; Box 1 is 18400 and Box 2 is 1210.",
    }, follow_redirects=False)
    assert response.status_code == 303
    final = service.get_state(rid)
    assert final["status"] == "complete" and final["review_passed"], [(e["kind"], e["reason"]) for e in final["escalations"] if e["status"] == "open"]
    assert final["lines"]["1z"] == 18400 and final["lines"]["25a"] == 1210
    assert final["extracted"][upload["id"]]["method"] == "staff-transcription"
    assert final["documents_in"] == out["documents_in"]
    pdf = client.get(f"/returns/{rid}/form.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")
    reader = PdfReader(io.BytesIO(pdf.content))
    assert reader.get_fields()[field(1, "f1_57[0]")]["/V"] == "18,400.00"
