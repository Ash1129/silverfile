"""The review -> revise cycle: injected faults the Review agent must catch, the
bound on the cycle, and the rule checks in isolation."""
from __future__ import annotations

import copy

import pytest

from conftest import case1_document_list, interrupts, load_intake, resolve, run_prefilled, CASE1
from silverfile.agents.review import independent_recompute, rule_checks
from silverfile.state import MAX_REVIEW_ROUNDS


@pytest.fixture(scope="module")
def clean_state():
    _, _, out = run_prefilled("review-clean", case1_document_list(), load_intake(CASE1))
    assert out["status"] == "complete"
    return out


def _checks(findings) -> set[str]:
    return {f["check"] for f in findings}


# ---------------------------------------------------------------- injected faults through the graph


def test_missing_provenance_is_caught_once_and_the_recompute_passes():
    g, cfg, out = run_prefilled("drop-prov", case1_document_list(), load_intake(CASE1), fault_injection={"drop_provenance_for": "6b", "rounds": 1})
    rounds = out["review_history"]
    assert [r["round"] for r in rounds] == [1, 2]
    assert rounds[0]["passed"] is False
    assert [f["check"] for f in rounds[0]["findings"]] == ["provenance-missing"]
    assert rounds[0]["findings"][0]["line"] == "6b"
    assert rounds[0]["findings"][0]["send_to"] == "computation"
    assert rounds[1]["passed"] is True and rounds[1]["findings"] == []
    assert out["computation_version"] == 2
    assert out["review_round"] == 2
    assert out["review_passed"] is True
    assert out["status"] == "complete"
    assert "6b" in out["provenance"]  # the second pass restored it
    assert out["lines"]["6b"] == 13_205.84
    assert interrupts(g, cfg) == []
    assert [e for e in out["escalations"] if e["status"] == "open"] == []


def test_persistent_corruption_hits_the_review_bound_then_escalates():
    g, cfg, out = run_prefilled("corrupt-9", case1_document_list(), load_intake(CASE1), fault_injection={"corrupt_line": "9", "delta": 100, "rounds": 99})
    rounds = out["review_history"]
    assert len(rounds) == MAX_REVIEW_ROUNDS
    assert all(r["passed"] is False for r in rounds)
    for r in rounds:
        assert {"provenance-mismatch", "recompute-mismatch"} <= _checks(r["findings"])
        assert all(f["line"] == "9" for f in r["findings"])
    assert out["computation_version"] == MAX_REVIEW_ROUNDS
    assert out["status"] == "waiting_human"
    assert out["loop_guard_tripped"] is False
    open_ = [e for e in out["escalations"] if e["status"] == "open"]
    assert [e["kind"] for e in open_] == ["review_unresolved"]
    assert open_[0]["id"] == f"esc-review-{MAX_REVIEW_ROUNDS}"
    assert "did not pass after 3 round(s)" in open_[0]["reason"]
    pending = interrupts(g, cfg)
    assert len(pending) == 1 and pending[0]["escalation"]["kind"] == "review_unresolved"
    # the draft still shows the shifted line so the preparer sees exactly what review rejected
    assert out["lines"]["9"] == 46_748.01


def test_accepting_an_unresolved_review_cannot_complete_an_unsourced_amount():
    """An unsourced finding remains blocking until remediation and explicit resolution."""
    g, cfg, out = run_prefilled("corrupt-accept-unsourced", case1_document_list(), load_intake(CASE1), fault_injection={"corrupt_line": "9", "delta": 100, "rounds": 99})
    assert out["status"] == "waiting_human"
    final = resolve(g, cfg, "accept", note="shift noted for the preparer")
    assert final["status"] == "waiting_human"
    assert final["blocking_review"]["status"] == "open"
    assert final["hitl_log"][-1]["result"] == "no"
    assert not final.get("provenance_seal")


def test_accepting_a_sourced_but_unresolved_review_does_not_complete_the_draft():
    # Matching provenance does not remove a blocking independent-recomputation finding.
    g, cfg, out = run_prefilled("corrupt-accept", case1_document_list(), load_intake(CASE1), fault_injection={"corrupt_line": "9", "delta": 100, "rounds": 99, "with_record": True})
    assert out["status"] == "waiting_human"
    final = resolve(g, cfg, "accept", note="shift noted for the preparer")
    assert final["status"] == "waiting_human"
    assert not final.get("provenance_seal")
    assert final["review_passed"] is False
    assert final["review_round"] == MAX_REVIEW_ROUNDS  # no further review rounds ran
    assert final["computation_version"] == MAX_REVIEW_ROUNDS
    assert [e["status"] for e in final["escalations"]] == ["open"]
    assert final["blocking_review"]["status"] == "open"
    assert interrupts(g, cfg)[0]["required_actions"]


def test_review_never_changes_a_figure(clean_state):
    _, _, out = run_prefilled("corrupt-readonly", case1_document_list(), load_intake(CASE1), fault_injection={"corrupt_line": "9", "delta": 100, "rounds": 99})
    # review reports the corrected value but the draft line stays what computation wrote
    assert out["review_history"][-1]["recomputed_lines"]["9"] == clean_state["lines"]["9"]
    assert out["lines"]["9"] == clean_state["lines"]["9"] + 100


# ---------------------------------------------------------------- rule_checks in isolation


def test_rule_checks_pass_on_a_clean_draft(clean_state):
    findings, recomputed = rule_checks(clean_state)
    assert findings == []
    for line, val in recomputed.items():
        assert clean_state["lines"][line] == val


def test_independent_recompute_matches_the_computation_agent(clean_state):
    recomputed = independent_recompute(clean_state["lines"], "single", clean_state["eligibility"])
    assert recomputed["6b"] == 13_205.84
    assert recomputed["15"] == 22_898.01
    assert recomputed["16"] == 2_509.26
    assert recomputed["34"] == 100.74 and recomputed["37"] == 0.0


def test_rule_checks_catch_taxable_benefits_above_total(clean_state):
    state = copy.deepcopy(clean_state)
    state["lines"]["6b"] = state["lines"]["6a"] + 1.0
    checks = _checks(rule_checks(state)[0])
    assert "taxable-exceeds-total" in checks
    assert "recompute-mismatch" in checks  # the worksheet disagrees too


def test_rule_checks_catch_a_provenance_amount_mismatch(clean_state):
    state = copy.deepcopy(clean_state)
    state["provenance"]["1z"]["amount"] = 1.0
    findings, _ = rule_checks(state)
    hits = [f for f in findings if f["check"] == "provenance-mismatch"]
    assert len(hits) == 1 and hits[0]["line"] == "1z" and hits[0]["severity"] == "high"


def test_rule_checks_catch_missing_provenance(clean_state):
    state = copy.deepcopy(clean_state)
    del state["provenance"]["16"]
    findings, _ = rule_checks(state)
    assert [f["line"] for f in findings if f["check"] == "provenance-missing"] == ["16"]


def test_rule_checks_catch_an_out_of_scope_document(clean_state):
    state = copy.deepcopy(clean_state)
    state["extracted"]["nec-1"] = {"id": "nec-1", "kind": "1099-NEC", "version": 1, "fields": {}, "issues": [], "out_of_scope": True}
    findings, _ = rule_checks(state)
    hits = [f for f in findings if f["check"] == "out-of-scope-document"]
    assert len(hits) == 1
    assert hits[0]["send_to"] == "human" and hits[0]["fixable"] is False


def test_rule_checks_catch_an_open_escalation(clean_state):
    state = copy.deepcopy(clean_state)
    state["escalations"] = [{"id": "esc-x", "kind": "conflict", "status": "open", "reason": "test"}]
    findings, _ = rule_checks(state)
    hits = [f for f in findings if f["check"] == "open-escalation"]
    assert len(hits) == 1 and "conflict" in hits[0]["detail"] and hits[0]["send_to"] == "human"


def test_rule_checks_catch_a_1099r_with_box_2b(clean_state):
    state = copy.deepcopy(clean_state)
    state["extracted"]["r-1"]["fields"]["taxable_not_determined"] = True
    assert "r-box-2b" in _checks(rule_checks(state)[0])


def test_rule_checks_with_no_lines():
    findings, recomputed = rule_checks({"lines": {}})
    assert [f["check"] for f in findings] == ["no-computation"] and recomputed == {}
