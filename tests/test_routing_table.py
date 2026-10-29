"""The Orchestrator's routing table, one test per row.

`decide_next` in agents/orchestrator.py is a pure function of the return's state:
it reads the state and names the next agent (or an exit). These tests walk the
table from the top. Rows are checked in the order the code checks them, so the
priority tests at the bottom prove that an earlier row wins over a later one.

(tests/test_decision_table.py is a different thing: it checks the tax rules in
tax_math. The loop-guard rows live in tests/test_loop_guard.py.)

Author: Mahika Arora (Task division and management)
"""
from __future__ import annotations

import copy

import pytest

from silverfile.agents.orchestrator import decide_next, orchestrator_node
from silverfile.state import MAX_REVIEW_ROUNDS

ELIGIBLE = {"eligible_1040sr": True, "in_scope": True, "chart_applies": True}
OPEN_ESC = [{"id": "esc-1", "kind": "advice_request", "status": "open"}]
RESOLVED_ESC = [{"id": "esc-1", "kind": "advice_request", "status": "resolved"}]


def state(**overrides):
    """A return that is ready for review: every earlier step done, nothing pending."""
    base = {
        "intake_complete": True,
        "extraction_done": True,
        "eligibility": dict(ELIGIBLE),
        "lines": {"1z": 1},
        "review_round": 0,
        "review_passed": False,
        "revise_target": None,
        "handoffs": [],
        "mailbox": [],
        "escalations": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- the end states


@pytest.mark.parametrize("status", ["complete", "halted"])
def test_finished_returns_end(status):
    assert decide_next(state(status=status)) == "end"


def test_a_finished_return_ends_even_with_an_open_escalation():
    assert decide_next(state(status="complete", escalations=OPEN_ESC)) == "end"


# ---------------------------------------------------------------- escalations and halts


def test_open_escalation_goes_to_a_human():
    assert decide_next(state(escalations=OPEN_ESC)) == "escalation"


def test_resolved_escalation_is_ignored():
    assert decide_next(state(escalations=RESOLVED_ESC)) == "review"


def test_halted_reason_goes_to_finalize():
    assert decide_next(state(halted_reason="a person took over")) == "finalize"


# ---------------------------------------------------------------- the working steps, in order


def test_intake_first_when_the_basics_are_incomplete():
    assert decide_next(state(intake_complete=False, extraction_done=False, eligibility=None, lines={})) == "intake"


def test_a_brand_new_return_starts_at_intake():
    assert decide_next({}) == "intake"


def test_extraction_when_documents_are_not_read():
    assert decide_next(state(extraction_done=False, eligibility=None, lines={})) == "extraction"


def test_eligibility_when_it_has_not_run():
    assert decide_next(state(eligibility=None, lines={})) == "eligibility"


def test_eligibility_again_when_review_asks_for_it():
    assert decide_next(state(revise_target="eligibility")) == "eligibility"


@pytest.mark.parametrize(
    "eligibility",
    [
        {"eligible_1040sr": False, "in_scope": True, "chart_applies": True},
        {"eligible_1040sr": True, "in_scope": False, "chart_applies": True},
        {"eligible_1040sr": True, "in_scope": True, "chart_applies": False},
    ],
    ids=["not-eligible", "out-of-scope", "chart-does-not-apply"],
)
def test_not_eligible_or_out_of_scope_goes_to_finalize(eligibility):
    # reached only once any escalation was resolved by a person taking the return over
    assert decide_next(state(eligibility=eligibility, lines={})) == "finalize"


def test_extraction_again_when_review_asks_for_it():
    assert decide_next(state(revise_target="extraction")) == "extraction"


def test_computation_when_no_lines_exist_yet():
    assert decide_next(state(lines={})) == "computation"


def test_computation_again_when_review_asks_for_it():
    assert decide_next(state(revise_target="computation")) == "computation"


def test_a_review_finding_aimed_at_intake_currently_goes_to_computation():
    # Documents current behaviour (orchestrator.py, the `revise_target == "intake"` clause):
    # Intake is not re-run, because Intake is the only agent that talks to the taxpayer.
    # If the team decides Intake should be re-run, this is the test to change.
    assert decide_next(state(revise_target="intake")) == "computation"


# ---------------------------------------------------------------- the review exits


def test_review_runs_when_the_draft_is_assembled_and_unreviewed():
    assert decide_next(state()) == "review"


def test_review_passed_goes_to_finalize():
    assert decide_next(state(review_round=1, review_passed=True)) == "finalize"


def test_a_finding_that_needs_a_person_goes_to_review_unresolved():
    assert decide_next(state(review_round=1, revise_target="human")) == "review_unresolved"


@pytest.mark.parametrize("rounds", [MAX_REVIEW_ROUNDS, MAX_REVIEW_ROUNDS + 1])
def test_the_review_bound_ends_the_cycle(rounds):
    assert decide_next(state(review_round=rounds)) == "review_unresolved"


def test_below_the_bound_the_cycle_continues():
    assert decide_next(state(review_round=MAX_REVIEW_ROUNDS - 1)) == "review"


def test_review_passing_beats_the_round_bound():
    assert decide_next(state(review_round=MAX_REVIEW_ROUNDS, review_passed=True)) == "finalize"


# ---------------------------------------------------------------- human review: recompute and resolution check
# Added after the human-review change (Guardrails A): two rows sit directly under the end states, so a
# person's edit or Continue is carried through before anything else is routed.


@pytest.mark.parametrize("stage", ["eligibility", "computation", "review"])
def test_a_recompute_stage_routes_to_that_agent(stage):
    assert decide_next(state(recompute_stage=stage)) == stage


def test_a_pending_resolution_goes_to_the_resolution_check():
    assert decide_next(state(resolution_request={"escalation_id": "esc-1", "action": "continue"})) == "resolution_check"


def test_recompute_beats_a_pending_resolution():
    s = state(recompute_stage="computation", resolution_request={"escalation_id": "esc-1", "action": "continue"})
    assert decide_next(s) == "computation"


@pytest.mark.parametrize("extra", [{"escalations": OPEN_ESC}, {"halted_reason": "x"}, {"revise_target": "human"}])
def test_human_review_steps_beat_escalation_halt_and_review_findings(extra):
    assert decide_next(state(recompute_stage="eligibility", **extra)) == "eligibility"
    assert decide_next(state(resolution_request={"escalation_id": "esc-1", "action": "continue"}, **extra)) == "resolution_check"


@pytest.mark.parametrize("status", ["complete", "halted"])
def test_a_finished_return_ends_even_mid_recompute(status):
    assert decide_next(state(status=status, recompute_stage="computation")) == "end"
    assert decide_next(state(status=status, resolution_request={"escalation_id": "esc-1", "action": "continue"})) == "end"


def test_node_follows_a_recompute_stage_and_numbers_the_computation_attempt():
    out = orchestrator_node(state(recompute_stage="computation", computation_version=2))
    assert out["next_agent"] == "computation" and out["computation_version"] == 3


def test_node_hands_a_pending_resolution_to_the_resolution_check():
    out = orchestrator_node(state(resolution_request={"escalation_id": "esc-1", "action": "continue"}))
    assert out["next_agent"] == "resolution_check"


# ---------------------------------------------------------------- priority: earlier rows win


def test_escalation_beats_every_later_step():
    s = state(escalations=OPEN_ESC, intake_complete=False, extraction_done=False, eligibility=None, lines={})
    assert decide_next(s) == "escalation"


def test_escalation_beats_a_halt():
    assert decide_next(state(escalations=OPEN_ESC, halted_reason="x")) == "escalation"


def test_halt_beats_intake():
    assert decide_next(state(halted_reason="x", intake_complete=False)) == "finalize"


def test_intake_beats_extraction():
    assert decide_next(state(intake_complete=False, extraction_done=False)) == "intake"


def test_extraction_beats_eligibility():
    assert decide_next(state(extraction_done=False, eligibility=None)) == "extraction"


def test_a_revise_request_for_eligibility_beats_the_scope_exit():
    s = state(eligibility={"eligible_1040sr": False, "in_scope": True, "chart_applies": True}, revise_target="eligibility")
    assert decide_next(s) == "eligibility"


def test_the_scope_exit_beats_a_computation_request():
    s = state(eligibility={"eligible_1040sr": True, "in_scope": False, "chart_applies": True}, lines={})
    assert decide_next(s) == "finalize"


# ---------------------------------------------------------------- decide_next is a pure function


def test_decide_next_does_not_change_the_state():
    s = state(escalations=OPEN_ESC, review_round=2, revise_target="computation")
    before = copy.deepcopy(s)
    decide_next(s)
    assert s == before


def test_same_state_gives_the_same_answer():
    s = state(review_round=1)
    assert {decide_next(s) for _ in range(5)} == {"review"}


# ---------------------------------------------------------------- orchestrator_node acts on the decision


def test_node_numbers_each_computation_attempt():
    out = orchestrator_node(state(lines={}, computation_version=1))
    assert out["next_agent"] == "computation"
    assert out["computation_version"] == 2
    assert out["status"] == "computing"


def test_node_clears_the_revise_request_once_it_acts_on_it():
    out = orchestrator_node(state(revise_target="computation"))
    assert out["next_agent"] == "computation"
    assert out["revise_target"] is None


def test_node_raises_a_review_unresolved_escalation_at_the_bound():
    out = orchestrator_node(state(review_round=MAX_REVIEW_ROUNDS, revise_reasons=["high: line 6b does not match"]))
    assert out["next_agent"] == "review_unresolved"
    assert out["status"] == "waiting_human"
    assert [e["kind"] for e in out["escalations"]] == ["review_unresolved"]
    assert out["escalations"][0]["raised_by"] == "review"


def test_node_sets_the_status_for_each_working_agent():
    expected = {
        "intake": state(intake_complete=False),
        "extracting": state(extraction_done=False),
        "eligibility": state(eligibility=None),
        "review": state(),
    }
    for status, s in expected.items():
        assert orchestrator_node(s)["status"] == status
