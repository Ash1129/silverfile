"""Orchestrator: decides which agent acts next and owns termination.

It never extracts, computes or talks to the taxpayer. Its routing is a pure
function of state (conditional routing, not a fixed sequence). It also runs the
loop guard: a repeating handoff pattern, or too many handoffs, is broken by
escalating to a human and halting.
"""
from __future__ import annotations

from typing import Any

from langgraph.types import Send

from ..provenance import now_iso
from ..state import LOOP_PATTERN_REPEATS, MAX_HANDOFFS, MAX_REVIEW_ROUNDS, Escalation, ReturnState

GUARDED = ("extraction", "eligibility", "computation", "review", "escalation")  # intake turns are legitimate repetition


def detect_loop(handoffs: list[str]) -> str | None:
    """Return a description of a pathological handoff cycle, or None.

    Two detectors: an absolute ceiling, and a repeated short pattern (length 2
    or 3) among the guarded agents, e.g. eligibility->computation->eligibility->
    computation->... LOOP_PATTERN_REPEATS times without reaching review.
    """
    if "reset" in handoffs:  # a person resolved an earlier loop-guard stop; judge only what happened since
        handoffs = handoffs[len(handoffs) - handoffs[::-1].index("reset"):]
    seq = [h for h in handoffs if h in GUARDED]
    if len(seq) > MAX_HANDOFFS:
        return f"more than {MAX_HANDOFFS} handoffs between the working agents"
    for n in (2, 3):
        window = n * LOOP_PATTERN_REPEATS
        if len(seq) >= window:
            tail = seq[-window:]
            pattern = tail[:n]
            if set(pattern) == {"computation", "review"}:
                continue  # the review->revise cycle has its own tested bound (MAX_REVIEW_ROUNDS)
            if len(set(pattern)) == n and all(tail[i] == pattern[i % n] for i in range(window)):
                return f"pattern {'->'.join(pattern)} repeated {LOOP_PATTERN_REPEATS} times"
    return None


def _open_escalations(state: ReturnState) -> list[Escalation]:
    return [e for e in state.get("escalations", []) if e.get("status") == "open"]


def decide_next(state: ReturnState) -> str:
    """The orchestrator's decision, as a label. `route` turns it into edges."""
    if state.get("status") in ("complete", "halted"):
        return "end"
    fault = state.get("fault_injection") or {}
    if fault.get("ping_pong") and state.get("intake_complete") and not state.get("loop_guard_tripped"):
        # test hook: a pathological handoff cycle between two agents
        last = [h for h in state.get("handoffs", []) if h in ("eligibility", "computation")]
        if detect_loop(state.get("handoffs", [])):
            return "loop_guard"
        return "computation" if (last and last[-1] == "eligibility") else "eligibility"
    if detect_loop(state.get("handoffs", [])) and not state.get("loop_guard_tripped"):
        return "loop_guard"
    if state.get("loop_guard_tripped"):
        return "escalation" if _open_escalations(state) else "finalize"
    if _open_escalations(state):
        return "escalation"
    if state.get("halted_reason"):
        return "finalize"
    if not state.get("intake_complete"):
        return "intake"
    if not state.get("extraction_done"):
        return "extraction"
    elig = state.get("eligibility")
    if not elig or state.get("revise_target") == "eligibility":
        return "eligibility"
    if not elig.get("eligible_1040sr") or not elig.get("in_scope") or not elig.get("chart_applies", True):
        return "finalize"  # the escalation was resolved by a person taking the return over
    if state.get("revise_target") == "extraction":
        return "extraction"
    if not state.get("lines") or state.get("revise_target") == "computation" or (state.get("revise_target") == "intake"):
        return "computation"
    if state.get("review_passed"):
        return "finalize"
    if state.get("revise_target") == "human" or int(state.get("review_round", 0)) >= MAX_REVIEW_ROUNDS:
        return "review_unresolved"
    return "review"


def orchestrator_node(state: ReturnState) -> dict[str, Any]:
    nxt = decide_next(state)
    out: dict[str, Any] = {"next_agent": nxt, "handoffs": ["orchestrator"], "events": [f"orchestrator -> {nxt}"]}
    status_for = {"intake": "intake", "extraction": "extracting", "eligibility": "eligibility", "computation": "computing", "review": "review", "escalation": "waiting_human"}
    if nxt in status_for:
        out["status"] = status_for[nxt]
    if nxt == "loop_guard":
        why = detect_loop(state.get("handoffs", [])) or "loop"
        out["loop_guard_tripped"] = True
        out["escalations"] = [Escalation(id="esc-loop-guard", kind="loop_guard", reason=f"The agents were handing the return back and forth without progress ({why}). The orchestrator stopped the cycle.", raised_by="orchestrator", raised_at=now_iso(), details={"handoffs": state.get("handoffs", [])[-12:]}, status="open", resolution=None, resolved_by=None, resolved_at=None)]
        out["events"].append(f"LOOP GUARD tripped: {why}")
        out["status"] = "waiting_human"
    if nxt == "review_unresolved":
        reasons = state.get("revise_reasons") or []
        out["escalations"] = [Escalation(id=f"esc-review-{state.get('review_round', 0)}", kind="review_unresolved", reason=f"Review did not pass after {state.get('review_round', 0)} round(s). Findings: " + "; ".join(reasons[:6]), raised_by="review", raised_at=now_iso(), details={"findings": reasons}, status="open", resolution=None, resolved_by=None, resolved_at=None)]
        out["status"] = "waiting_human"
    if nxt == "extraction" and state.get("revise_target") == "extraction":
        out["extraction_done"] = False
    if nxt in ("computation", "eligibility", "extraction") and state.get("revise_target"):
        out["revise_target"] = None
    return out


def route(state: ReturnState):
    """Conditional edges from the orchestrator. Extraction fans out one Send per document."""
    nxt = state.get("next_agent") or decide_next(state)
    if nxt == "extraction":
        docs = state.get("documents_in") or []
        version = 1 + max([int(d.get("version", 1)) for d in (state.get("extracted") or {}).values()] or [0]) if state.get("extracted") else 1
        if not docs:
            return "aggregate_extraction"
        return [Send("extract_document", {"document": {**d, "version": version}}) for d in docs]
    if nxt in ("loop_guard", "review_unresolved"):
        return "orchestrator"  # the node already recorded the escalation; re-route to it
    if nxt == "end":
        return "finalize"
    return nxt
