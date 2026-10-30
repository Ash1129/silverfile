"""Human-in-the-loop escalations raised by eligibility and resolved through the
escalation interrupt."""
from __future__ import annotations

import pytest

from conftest import (
    CASE1, CASE2, CASE3, CASE4, case1_document_list, case2_document_list, document, interrupts, load_intake, resolve, run_prefilled,
)
from silverfile.agents.eligibility import run_eligibility
from silverfile.security.validate import unknown_fact_actions


def _open_kinds(state) -> list[str]:
    return [e["kind"] for e in state["escalations"] if e["status"] == "open"]


# ---------------------------------------------------------------- deduction-chart confirmation contract

def _chart_intake(filing_status="single"):
    facts = load_intake(CASE2 if filing_status == "mfs" else CASE1)
    return {**facts, "filing_status": filing_status, "spouse_died_in_year": False}


@pytest.mark.parametrize("field,filing_status", [("dual_status_alien", "single"), ("mfs_spouse_itemizes", "mfs")])
@pytest.mark.parametrize("value", [None, "false", 0, ""])
def test_unconfirmed_chart_flags_are_unknown_not_negative(field, filing_status, value):
    facts = {**_chart_intake(filing_status), field: value}
    result = run_eligibility({"intake": facts})
    assert result["eligibility"]["chart_applies"] is None
    event = next(e for e in result["escalations"] if e["kind"] == "uncertain_fact")
    assert event["details"]["required_actions"][0]["field"] == field
    assert event["details"]["citation"] == "REF-SD-4"


@pytest.mark.parametrize("field,filing_status", [("dual_status_alien", "single"), ("mfs_spouse_itemizes", "mfs")])
@pytest.mark.parametrize("missing", [True, False], ids=["missing", "null"])
def test_missing_chart_fact_blocks_continue_and_accept_until_staff_confirmation(field, filing_status, missing):
    facts = _chart_intake(filing_status)
    if missing:
        facts.pop(field)
    else:
        facts[field] = None
        facts["_unknown_fields"] = [field]
    docs = case2_document_list() if filing_status == "mfs" else case1_document_list()
    g, cfg, out = run_prefilled(f"chart-missing-{field}", docs, facts)
    assert out["status"] == "waiting_human" and not out["lines"]
    assert out["computation_version"] == 0
    assert _open_kinds(out) == ["uncertain_fact"]
    assert interrupts(g, cfg)[0]["escalation"]["details"]["required_actions"][0]["field"] == field
    out = resolve(g, cfg, "continue")
    assert out["status"] == "waiting_human" and out["hitl_log"][-1]["result"] == "no"
    assert any(a.get("field") == field for a in out["hitl_log"][-1]["required_actions"])
    out = resolve(g, cfg, "accept")
    assert out["status"] == "waiting_human" and not out["lines"]
    final = resolve(g, cfg, "continue", note="Preparer confirmed the exception does not apply.", facts_patch={field: False})
    assert final["status"] == "complete" and final["eligibility"]["chart_applies"] is True
    assert final["intake"][field] is False
    assert final["computation_version"] == 1 and final["provenance_seal"]
    assert any(e["event"] == "continue_check" and e["result"] == "yes" for e in final["hitl_log"])


@pytest.mark.parametrize("field,filing_status", [("can_be_claimed_as_dependent", "single"), ("dual_status_alien", "single"), ("mfs_spouse_itemizes", "mfs")])
def test_confirmed_chart_exceptions_cannot_be_overridden_by_continue_or_accept(field, filing_status):
    facts = {**_chart_intake(filing_status), field: True}
    docs = case2_document_list() if filing_status == "mfs" else case1_document_list()
    g, cfg, out = run_prefilled(f"chart-true-{field}", docs, facts)
    assert out["status"] == "waiting_human" and not out["lines"]
    assert _open_kinds(out) == ["chart_not_applicable"]
    for action in ("continue", "accept"):
        out = resolve(g, cfg, action)
        assert out["status"] == "waiting_human" and not out["lines"]
        assert out["hitl_log"][-1]["result"] == "no"
    assert resolve(g, cfg, "halt", note="The confirmed exception requires a preparer.")["status"] == "halted"


@pytest.mark.parametrize("filing_status", ["single", "mfj", "hoh", "qss"])
def test_spouse_itemising_flag_and_its_unknown_marker_do_not_apply_outside_mfs(filing_status):
    facts = {**_chart_intake(), "filing_status": filing_status, "mfs_spouse_itemizes": True,
             "_unknown_fields": ["mfs_spouse_itemizes"]}
    assert unknown_fact_actions({"intake": facts}) == []
    assert run_eligibility({"intake": facts})["eligibility"]["chart_applies"] is True


def test_missing_chart_answers_do_not_interrupt_unfinished_intake():
    assert unknown_fact_actions({"intake": {"filing_status": "mfs"}}) == []
    facts = {"filing_status": "mfs", "dual_status_alien": None}
    assert [a["field"] for a in unknown_fact_actions({"intake": facts})] == ["dual_status_alien"]
    facts = {"filing_status": "mfs"}
    assert {a["field"] for a in unknown_fact_actions({"intake": facts, "intake_complete": True})} == {"dual_status_alien", "mfs_spouse_itemizes"}


@pytest.mark.parametrize("field,filing_status", [("dual_status_alien", "single"), ("mfs_spouse_itemizes", "mfs")])
def test_computation_refuses_missing_chart_facts_even_with_stale_eligibility(field, filing_status):
    from silverfile.agents.computation import run_computation
    facts = _chart_intake(filing_status)
    facts.pop(field)
    out = run_computation({"intake": facts, "eligibility": {"chart_applies": True}})
    assert "lines" not in out
    assert out["escalations"][0]["kind"] == "uncertain_fact"
    assert out["escalations"][0]["details"]["required_actions"][0]["field"] == field


def test_finalization_refuses_unknown_chart_fact_despite_a_stale_passing_review():
    from silverfile.graph import finalize_node
    _, _, out = run_prefilled("chart-finalize", case1_document_list(), load_intake(CASE1))
    assert out["status"] == "complete"
    out["intake"].pop("dual_status_alien")
    result = finalize_node(out)
    assert result["status"] == "halted" and "unknown" in result["halted_reason"]
    assert "provenance_seal" not in result


# ---------------------------------------------------------------- case2: spouse died during the year


def test_spouse_died_pauses_on_filing_status_and_mfj_completes():
    g, cfg, out = run_prefilled("case2", case2_document_list(), load_intake(CASE2))
    assert out["status"] == "waiting_human"
    assert _open_kinds(out) == ["filing_status"]
    pending = interrupts(g, cfg)
    assert len(pending) == 1
    assert pending[0]["type"] == "escalation"
    assert pending[0]["escalation"]["kind"] == "filing_status"
    assert pending[0]["escalation"]["raised_by"] == "eligibility"
    assert "Harold Benson" in pending[0]["summary"]
    assert not out["lines"]  # nothing computed before the decision

    final = resolve(g, cfg, "continue", note="MFJ for the year of death", facts_patch={"filing_status": "mfj"})
    assert final["status"] == "complete"
    assert interrupts(g, cfg) == []
    assert final["intake"]["filing_status"] == "mfj"
    assert "_filing_status_unsure" not in final["intake"]
    assert final["eligibility"]["filing_status"] == "mfj"
    assert final["eligibility"]["box_count"] == 3  # taxpayer 65 + taxpayer blind + spouse 65
    assert final["eligibility"]["boxes_12d"] == {"taxpayer_65": True, "taxpayer_blind": True, "spouse_65": True, "spouse_blind": False}
    assert final["lines"]["12e"] == 36_300.0
    assert final["lines"]["13b"] == 12_000.0
    assert final["lines"]["6b"] == 7_462.47
    assert final["lines"]["15"] == 0.0 and final["lines"]["16"] == 0.0
    assert final["lines"]["34"] == 4_000.0
    assert [(e["kind"], e["status"]) for e in final["escalations"]] == [("filing_status", "resolved")]
    assert final["escalations"][0]["resolution"]["facts_patch"] == {"filing_status": "mfj"}
    assert final["review_passed"] is True


def test_spouse_died_halt_hands_the_return_over():
    g, cfg, out = run_prefilled("case2-halt", case2_document_list(), load_intake(CASE2))
    final = resolve(g, cfg, "halt", note="preparer will call")
    assert final["status"] == "halted"
    assert final["halted_reason"] == "filing_status: preparer will call"
    assert interrupts(g, cfg) == []


# ---------------------------------------------------------------- case3: out of scope (1099-NEC)


def _case3_intake():
    base = load_intake(CASE1)
    return {**base, "taxpayer": {**base["taxpayer"], "first_name": "Ruth", "last_name": "Adler"}, "documents_declared": ["SSA-1099"]}


def test_out_of_scope_document_pauses_and_halt_ends_halted():
    docs = [document("ssa-1", "SSA-1099", CASE3 / "ssa1099.json"), document("nec-1", "1099-NEC", CASE3 / "1099nec.json")]
    g, cfg, out = run_prefilled("case3", docs, _case3_intake())
    assert out["status"] == "waiting_human"
    assert _open_kinds(out) == ["out_of_scope"]
    assert out["extracted"]["nec-1"]["out_of_scope"] is True
    assert out["eligibility"]["in_scope"] is False
    assert out["eligibility"]["out_of_scope_items"] == ["nec-1: 1099-NEC (self-employment / nonemployee compensation (Schedule C))"]
    assert not out["lines"]
    pending = interrupts(g, cfg)
    assert pending[0]["escalation"]["kind"] == "out_of_scope"
    assert "Schedule C" in pending[0]["escalation"]["reason"]

    final = resolve(g, cfg, "halt", note="Schedule C: a preparer takes it")
    assert final["status"] == "halted"
    assert final["halted_reason"].startswith("out_of_scope:")
    assert interrupts(g, cfg) == []
    assert final["handoffs"][-1] == "finalize"


def test_out_of_scope_statement_in_intake_is_caught_without_a_document():
    intake = {**load_intake(CASE1), "other_income_sources": ["I rent out my late husband's cottage in the summer"]}
    _, _, out = run_prefilled("case3-statement", case1_document_list(), intake)
    assert out["status"] == "waiting_human"
    assert _open_kinds(out) == ["out_of_scope"]
    assert "Schedule E" in out["eligibility"]["out_of_scope_items"][0]


# ---------------------------------------------------------------- case4: 1099-R box 2b


def test_1099r_box_2b_pauses_on_r_box2b():
    intake = {**load_intake(CASE1), "taxpayer": {**load_intake(CASE1)["taxpayer"], "first_name": "Samuel", "last_name": "Ortega"}, "documents_declared": ["1099-R"]}
    g, cfg, out = run_prefilled("case4", [document("r-1", "1099-R", CASE4 / "1099r.json")], intake)
    assert out["status"] == "waiting_human"
    assert _open_kinds(out) == ["r_box2b"]
    assert out["extracted"]["r-1"]["fields"]["taxable_not_determined"] is True
    pending = interrupts(g, cfg)
    assert pending[0]["escalation"]["kind"] == "r_box2b"
    assert "box 2b" in pending[0]["escalation"]["reason"]
    assert not out["lines"]  # computation never ran, so taxable_1099r never raised
    final = resolve(g, cfg, "halt")
    assert final["status"] == "halted"


# ---------------------------------------------------------------- declared-but-missing document


def test_declared_document_missing_is_a_conflict():
    docs = [d for d in case1_document_list() if d["id"] != "r-1"]  # the 1099-R was declared but not supplied
    g, cfg, out = run_prefilled("missing-doc", docs, load_intake(CASE1))
    assert out["status"] == "waiting_human"
    assert _open_kinds(out) == ["conflict"]
    open_ = [e for e in out["escalations"] if e["status"] == "open"][0]
    assert open_["reason"] == "Taxpayer said they have a 1099-R but none was supplied."
    assert open_["raised_by"] == "eligibility"
    assert out["eligibility"]["conflicts"] == ["Taxpayer said they have a 1099-R but none was supplied."]
    assert interrupts(g, cfg)[0]["escalation"]["kind"] == "conflict"


def test_resolving_a_conflict_with_a_corrected_declaration_completes():
    docs = [d for d in case1_document_list() if d["id"] != "r-1"]
    g, cfg, out = run_prefilled("missing-doc-fix", docs, load_intake(CASE1))
    final = resolve(g, cfg, "continue", note="taxpayer confirmed there is no pension", facts_patch={"documents_declared": ["W-2", "SSA-1099", "1099-INT"]})
    assert final["status"] == "complete"
    assert final["eligibility"]["conflicts"] == []
    assert final["lines"]["5a"] == 0.0 and final["lines"]["5b"] == 0.0
    assert final["lines"]["1z"] == 18_400.0


# ---------------------------------------------------------------- exploitation signals


def test_third_party_without_authority_is_an_exploitation_escalation():
    intake = {**load_intake(CASE1), "answering_party": "third_party_no_authority", "third_party_authority": "I'm her nephew, she asked me to do it"}
    g, cfg, out = run_prefilled("third-party", case1_document_list(), intake)
    assert out["status"] == "waiting_human"
    open_ = [e for e in out["escalations"] if e["status"] == "open"]
    assert [e["kind"] for e in open_] == ["exploitation"]
    assert open_[0]["raised_by"] == "eligibility"
    assert "third party" in open_[0]["reason"].lower()
    assert interrupts(g, cfg)[0]["escalation"]["kind"] == "exploitation"
    assert not out["lines"]


def test_refund_account_not_the_taxpayers_own_is_an_exploitation_escalation():
    intake = {**load_intake(CASE1), "refund_account_is_own": False, "refund_account_description": "the account my nephew set up for me"}
    g, cfg, out = run_prefilled("refund-account", case1_document_list(), intake)
    assert out["status"] == "waiting_human"
    open_ = [e for e in out["escalations"] if e["status"] == "open"]
    assert [e["kind"] for e in open_] == ["exploitation"]
    assert open_[0]["raised_by"] == "eligibility"
    assert "nephew set up" in open_[0]["reason"]
    assert interrupts(g, cfg)[0]["escalation"]["kind"] == "exploitation"


def test_eligibility_node_raises_both_exploitation_signals_with_citations():
    intake = {**load_intake(CASE1), "answering_party": "third_party_no_authority", "refund_account_is_own": False}
    out = run_eligibility({"intake": intake, "extracted": {}, "extraction_done": False, "escalations": []})
    kinds = [e["kind"] for e in out["escalations"]]
    assert kinds == ["exploitation", "exploitation"]
    assert all(e["details"]["citation"] == "REF-ES-1" for e in out["escalations"])
    assert all(e["status"] == "open" for e in out["escalations"])


def test_eligibility_does_not_raise_the_same_escalation_twice():
    intake = {**load_intake(CASE1), "answering_party": "third_party_no_authority"}
    first = run_eligibility({"intake": intake, "extracted": {}, "extraction_done": False, "escalations": []})
    again = run_eligibility({"intake": intake, "extracted": {}, "extraction_done": False, "escalations": first["escalations"]})
    assert len(first["escalations"]) == 1 and again["escalations"] == []


# ---------------------------------------------------------------- a clean return raises nothing


def test_clean_return_raises_no_escalation():
    g, cfg, out = run_prefilled("case1-clean", case1_document_list(), load_intake(CASE1))
    assert out["status"] == "complete"
    assert out["escalations"] == []
    assert interrupts(g, cfg) == []
    assert out["lines"]["1z"] == 18_400.0
    assert out["lines"]["6b"] == 13_205.84
    assert out["lines"]["15"] == 22_898.01
    assert out["lines"]["16"] == 2_509.26
    assert out["lines"]["34"] == 100.74 and out["lines"]["37"] == 0.0
    assert out["eligibility"]["citations"]  # every determination cites the reference corpus
    assert all(d.get("citation") for d in out["eligibility"]["determinations"])
