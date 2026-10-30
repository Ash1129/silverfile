"""The SilverFile graph: an orchestrator hub with six agent spokes.

Topology (hub and spoke with one fan-out):
  START -> orchestrator -+-> intake ------------------+
                         +-> extract_document (xN) -> aggregate_extraction -+
                         +-> eligibility ------------+
                         +-> computation ------------+---> orchestrator (again)
                         +-> review -----------------+
                         +-> escalation (interrupt) -+
                         +-> finalize -> END

Why this topology: the brief's business rule is six distinct agents that may not
be merged, with an orchestrator that decides who acts next and owns
termination. A hub makes every handoff explicit (and loggable for the loop
guard), keeps the review->revise cycle a real cycle through the hub, and lets
the fan-out for extraction be the one genuine parallel branch with an
aggregation step and a reducer (`merge_documents`).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .agents.computation import run_computation
from .agents.eligibility import run_eligibility
from .agents.escalation import accept_requirements, escalation_node, resolution_check_node
from .agents.extraction import aggregate_extraction_node, extract_document_node
from .agents.intake import run_intake
from .agents.orchestrator import orchestrator_node, route
from .agents.review import run_review
from .comms import communicating, communicating_branch
from .config import settings
from .provenance import now_iso
from .security import crypto, integrity
from .security.validate import provenance_problems, unknown_fact_actions
from .state import ReturnState
from .reconciliation import evidence_conflicts


def finalize_node(state: ReturnState) -> dict[str, Any]:
    if evidence_conflicts(state):
        return {"status": "halted", "halted_reason": "Conflicting evidence has not been resolved.", "handoffs": ["finalize"], "events": ["finalize: blocked by conflicting evidence"]}
    event = state.get("blocking_review")
    if state.get("recompute_stage") or (state.get("human_edit") or {}).get("status") in ("editing", "completed", "blocked") or (event and accept_requirements(state)):
        return {"status": "halted", "halted_reason": "Human review or fact editing has not completed its required actions.", "handoffs": ["finalize"], "events": ["finalize: blocked by incomplete human review"]}
    if state.get("halted_reason") or state.get("loop_guard_tripped"):
        return {"status": "halted", "handoffs": ["finalize"], "events": [f"finalize: halted ({state.get('halted_reason') or 'loop guard'})"]}
    elig = state.get("eligibility") or {}
    if not elig.get("eligible_1040sr") or not elig.get("in_scope") or not elig.get("chart_applies", True):
        return {"status": "halted", "halted_reason": "not eligible or out of scope; a Kushim preparer takes it from here", "handoffs": ["finalize"], "events": ["finalize: halted (eligibility/scope)"]}
    if unknown_fact_actions(state, require_chart_facts=True):
        return {"status": "halted", "halted_reason": "Required facts remain unknown; a Kushim preparer must confirm them.", "handoffs": ["finalize"], "events": ["finalize: blocked by unknown facts"]}
    # the last structural gate: no draft completes with an amount that is not sourced, whoever accepted it
    unsourced = provenance_problems(state)
    if unsourced:
        why = "; ".join(f"line {l}: {p}" for l, p in unsourced[:4])
        return {"status": "halted", "halted_reason": f"provenance guard: {why}", "handoffs": ["finalize"], "events": [f"finalize: halted, {len(unsourced)} unsourced amount(s)"]}
    sealed = integrity.seal(state)
    return {"status": "complete", "provenance_seal": sealed, "handoffs": ["finalize"], "events": [f"finalize: draft complete at {now_iso()} after {state.get('review_round', 0)} review round(s); provenance sealed ({sealed['lines']} lines, root {sealed['root'][:12]})"]}


def build_graph():
    # every spoke is wrapped so it speaks the message protocol and is held to its write contract (comms/)
    g = StateGraph(ReturnState)
    g.add_node("orchestrator", orchestrator_node)
    g.add_node("intake", communicating("intake", run_intake))
    g.add_node("extract_document", communicating_branch(extract_document_node))
    g.add_node("aggregate_extraction", communicating("extraction", aggregate_extraction_node))
    g.add_node("eligibility", communicating("eligibility", run_eligibility))
    g.add_node("computation", communicating("computation", run_computation))
    g.add_node("review", communicating("review", run_review))
    g.add_node("escalation", communicating("escalation", escalation_node))
    g.add_node("resolution_check", communicating("resolution_check", resolution_check_node))
    g.add_node("finalize", communicating("finalize", finalize_node))

    g.add_edge(START, "orchestrator")
    g.add_conditional_edges("orchestrator", route, ["intake", "extract_document", "aggregate_extraction", "eligibility", "computation", "review", "escalation", "resolution_check", "finalize", "orchestrator"])
    g.add_edge("extract_document", "aggregate_extraction")
    for spoke in ("intake", "aggregate_extraction", "eligibility", "computation", "review", "escalation", "resolution_check"):
        g.add_edge(spoke, "orchestrator")
    g.add_edge("finalize", END)
    return g


_compiled: dict[str, Any] = {}


def get_checkpointer(path: str | Path | None = None):
    """SQLite checkpoints, every blob encrypted with an authenticated cipher (security/crypto.py).

    An unencrypted or altered blob fails to load, so a checkpoint cannot be edited
    or forged without the data key. ":memory:" (CLI demos and tests) stays in the process.
    """
    if path is None:
        path = settings().checkpoint_db
    if str(path) == ":memory:":
        return InMemorySaver()
    fresh = not Path(path).exists()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    if fresh:
        crypto.restrict_file(Path(path))
    return SqliteSaver(conn, serde=crypto.checkpoint_serde())


def compile_graph(checkpointer=None):
    """Compile with a checkpointer (SQLite by default) so interrupts and resumes work."""
    cp = checkpointer if checkpointer is not None else get_checkpointer()
    return build_graph().compile(checkpointer=cp)


def app(path: str | Path | None = None):
    key = str(path or settings().checkpoint_db)
    if key not in _compiled:
        _compiled[key] = compile_graph(get_checkpointer(path))
    return _compiled[key]


def new_return_state(return_id: str, documents: list[dict[str, Any]] | None = None, fault_injection: dict[str, Any] | None = None, intake: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "return_id": return_id, "tax_year": 2025, "created_at": now_iso(), "status": "new",
        "intake": intake or {}, "intake_complete": bool(intake and intake.get("_confirmed")),
        "documents_in": documents or [], "extraction_done": False, "lines": {}, "provenance": {}, "computation_version": 0,
        "review_round": 0, "review_passed": False, "revise_target": None, "loop_guard_tripped": False, "fault_injection": fault_injection or {},
        "handoffs": [], "mailbox": [], "hitl_log": [], "blocking_review": None, "human_edit": None, "recompute_stage": None, "resolution_request": None, "events": [f"return {return_id} created"],
    }
