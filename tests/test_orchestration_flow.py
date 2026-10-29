"""The Orchestrator's decisions seen through the whole graph.

tests/test_routing_table.py checks `decide_next` row by row, on hand-made states.
These tests run real returns through the compiled graph and check the *sequence*
of agents that comes out: the hub-and-spoke shape (every worker hands back to the
Orchestrator), that the parallel extraction and the review loop each run the
number of times they should, and that the same return always takes the same route.

Author: Mahika Arora (Task division and management)
"""
from __future__ import annotations

from conftest import case1_document_list, load_intake, run_prefilled, CASE1
from silverfile.state import MAX_HANDOFFS, MAX_REVIEW_ROUNDS

CLEAN_ORDER = ["extraction", "eligibility", "computation", "review", "finalize"]


def _workers(out) -> list[str]:
    """The agents that acted, in order, without the Orchestrator's own entries."""
    return [h for h in out["handoffs"] if h != "orchestrator"]


def _decisions(out) -> list[str]:
    """What the Orchestrator decided each time it ran, from its own event log."""
    return [e.removeprefix("orchestrator -> ") for e in out["events"] if e.startswith("orchestrator -> ")]


def test_a_clean_return_visits_each_agent_once_in_order():
    _, _, out = run_prefilled("flow-clean", case1_document_list(), load_intake(CASE1))
    assert out["status"] == "complete"
    assert _workers(out) == CLEAN_ORDER
    assert _decisions(out) == CLEAN_ORDER  # what it decided is what then ran


def test_the_route_is_hub_and_spoke_every_worker_returns_to_the_orchestrator():
    _, _, out = run_prefilled("flow-hub", case1_document_list(), load_intake(CASE1))
    hs = out["handoffs"]
    assert hs[0] == "orchestrator"  # the graph's only entry point
    assert all(h == "orchestrator" for h in hs[0::2])  # the Orchestrator acts between every pair of workers
    assert all(h != "orchestrator" for h in hs[1::2])
    assert hs[-1] == "finalize"  # and the return ends at finalize, not at the hub


def test_a_prefilled_return_skips_intake_and_starts_at_extraction():
    _, _, out = run_prefilled("flow-entry", case1_document_list(), load_intake(CASE1))
    assert "intake" not in out["handoffs"]
    assert _workers(out)[0] == "extraction"


def test_extraction_runs_once_for_all_documents_not_once_per_document():
    _, _, out = run_prefilled("flow-fanout", case1_document_list(), load_intake(CASE1))
    assert len(out["extracted"]) == 4
    assert _workers(out).count("extraction") == 1  # four parallel branches, one aggregated handoff


def test_a_clean_return_never_needs_the_human_review_nodes():
    _, _, out = run_prefilled("flow-no-human", case1_document_list(), load_intake(CASE1))
    assert "escalation" not in out["handoffs"] and "resolution_check" not in out["handoffs"]
    assert out.get("recompute_stage") is None and out.get("resolution_request") is None


def test_one_review_round_when_the_draft_is_clean():
    _, _, out = run_prefilled("flow-one-round", case1_document_list(), load_intake(CASE1))
    assert out["review_round"] == 1 and out["review_passed"] is True
    assert _workers(out).count("review") == 1 and _workers(out).count("computation") == 1


def test_a_review_finding_sends_only_computation_back_not_the_whole_pipeline():
    # a provenance record is dropped once: Review fails round 1, Computation reruns, Review passes round 2
    _, _, out = run_prefilled("flow-revise", case1_document_list(), load_intake(CASE1), fault_injection={"drop_provenance_for": "6b", "rounds": 1})
    assert out["status"] == "complete"
    assert _workers(out) == ["extraction", "eligibility", "computation", "review", "computation", "review", "finalize"]
    assert _decisions(out) == _workers(out)


def test_a_review_loop_that_never_passes_is_stopped_by_its_own_bound_not_the_loop_guard():
    _, _, out = run_prefilled("flow-bound", case1_document_list(), load_intake(CASE1), fault_injection={"corrupt_line": "9", "delta": 100, "rounds": 99})
    w = _workers(out)
    assert w.count("review") == MAX_REVIEW_ROUNDS and w.count("computation") == MAX_REVIEW_ROUNDS
    assert w.count("extraction") == 1 and w.count("eligibility") == 1  # earlier steps are not repeated
    assert out["loop_guard_tripped"] is False  # the computation/review pair is bounded by MAX_REVIEW_ROUNDS
    assert out["status"] == "waiting_human"
    assert _decisions(out)[-2:] == ["review_unresolved", "escalation"]
    assert len(w) < MAX_HANDOFFS


def test_the_same_return_takes_the_same_route_every_time():
    _, _, first = run_prefilled("flow-repeat-a", case1_document_list(), load_intake(CASE1))
    _, _, second = run_prefilled("flow-repeat-b", case1_document_list(), load_intake(CASE1))
    assert first["handoffs"] == second["handoffs"]
    assert _decisions(first) == _decisions(second)
