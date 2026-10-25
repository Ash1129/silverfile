"""The orchestrator's loop guard: unit cases for detect_loop and the graph case
with the ping-pong fault injected."""
from __future__ import annotations

from conftest import case1_document_list, interrupts, load_intake, resolve, run_prefilled, CASE1
from silverfile.agents.orchestrator import decide_next, detect_loop
from silverfile.state import LOOP_PATTERN_REPEATS, MAX_HANDOFFS

CLEAN_RUN = ["orchestrator", "extraction", "orchestrator", "eligibility", "orchestrator", "computation", "orchestrator", "review", "orchestrator", "finalize"]


def _alternate(a: str, b: str, times: int, with_hub: bool = True) -> list[str]:
    pair = ["orchestrator", a, "orchestrator", b] if with_hub else [a, b]
    return pair * times


# ---------------------------------------------------------------- detect_loop


def test_no_loop_on_a_clean_run():
    assert detect_loop([]) is None
    assert detect_loop(CLEAN_RUN) is None


def test_eligibility_computation_ping_pong_trips_after_three_repeats():
    assert detect_loop(_alternate("eligibility", "computation", LOOP_PATTERN_REPEATS - 1)) is None
    why = detect_loop(_alternate("eligibility", "computation", LOOP_PATTERN_REPEATS))
    assert why == "pattern eligibility->computation repeated 3 times"


def test_orchestrator_entries_do_not_mask_the_pattern():
    assert detect_loop(_alternate("eligibility", "computation", LOOP_PATTERN_REPEATS, with_hub=False)) is not None


def test_review_revise_cycle_is_not_a_loop():
    # the review -> computation cycle has its own bound (MAX_REVIEW_ROUNDS) and must not trip the guard
    assert detect_loop(_alternate("computation", "review", LOOP_PATTERN_REPEATS)) is None
    assert detect_loop(_alternate("review", "computation", LOOP_PATTERN_REPEATS + 2)) is None


def test_three_agent_pattern_trips():
    seq = ["extraction", "eligibility", "computation"] * LOOP_PATTERN_REPEATS
    assert detect_loop(seq) == "pattern extraction->eligibility->computation repeated 3 times"


def test_reset_marker_clears_earlier_history():
    tripped = _alternate("eligibility", "computation", LOOP_PATTERN_REPEATS)
    assert detect_loop(tripped) is not None
    after_reset = [*tripped, "reset", *_alternate("eligibility", "computation", 1)]
    assert detect_loop(after_reset) is None
    # it trips again once the pattern repeats after the reset
    assert detect_loop([*tripped, "reset", *_alternate("eligibility", "computation", LOOP_PATTERN_REPEATS)]) is not None


def test_ceiling_counts_guarded_handoffs_only():
    assert detect_loop(["intake"] * 200) is None  # a long conversation is legitimate repetition
    assert detect_loop(["orchestrator"] * 200) is None
    assert detect_loop(["review"] * MAX_HANDOFFS) is None
    assert detect_loop(["review"] * (MAX_HANDOFFS + 1)) == f"more than {MAX_HANDOFFS} handoffs between the working agents"
    assert detect_loop(["intake"] * 100 + ["review"] * MAX_HANDOFFS) is None


# ---------------------------------------------------------------- decide_next around the guard


def test_decide_next_routes_to_loop_guard_then_escalation_then_finalize():
    base = {"intake_complete": True, "extraction_done": True, "handoffs": _alternate("eligibility", "computation", LOOP_PATTERN_REPEATS)}
    assert decide_next(base) == "loop_guard"
    tripped = {**base, "loop_guard_tripped": True, "escalations": [{"id": "esc-loop-guard", "kind": "loop_guard", "status": "open"}]}
    assert decide_next(tripped) == "escalation"
    assert decide_next({**tripped, "escalations": [{"id": "esc-loop-guard", "kind": "loop_guard", "status": "resolved"}]}) == "finalize"


def test_decide_next_ends_finished_returns():
    assert decide_next({"status": "complete"}) == "end"
    assert decide_next({"status": "halted"}) == "end"


# ---------------------------------------------------------------- the graph case


def test_ping_pong_fault_is_caught_and_a_halt_ends_the_return():
    g, cfg, out = run_prefilled("ping-pong", case1_document_list(), load_intake(CASE1), fault_injection={"ping_pong": True})
    assert out["status"] == "waiting_human"
    assert out["loop_guard_tripped"] is True
    open_ = [e for e in out["escalations"] if e["status"] == "open"]
    assert [e["kind"] for e in open_] == ["loop_guard"]
    assert open_[0]["raised_by"] == "orchestrator"
    assert "eligibility->computation" in open_[0]["reason"]
    pending = interrupts(g, cfg)
    assert len(pending) == 1 and pending[0]["type"] == "escalation" and pending[0]["escalation"]["kind"] == "loop_guard"
    # the guard stopped the cycle after exactly three repeats, before extraction or review ever ran
    guarded = [h for h in out["handoffs"] if h in ("eligibility", "computation")]
    assert guarded == ["eligibility", "computation"] * LOOP_PATTERN_REPEATS
    assert "review" not in out["handoffs"] and "extraction" not in out["handoffs"]
    assert out["review_round"] == 0
    assert not out.get("extracted")
    assert any(e.startswith("LOOP GUARD tripped") for e in out["events"])

    final = resolve(g, cfg, "halt", note="a person takes it from here")
    assert final["status"] == "halted"
    assert final["halted_reason"].startswith("loop_guard:")
    assert interrupts(g, cfg) == []
    assert [e["status"] for e in final["escalations"]] == ["resolved"]
    assert final["handoffs"][-1] == "finalize"
