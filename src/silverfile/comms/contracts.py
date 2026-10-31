"""Handoff contracts: what each agent is given and what it may write back.

Shared state is the transport, but not every agent may touch every key. The
write contract is enforced on every node's update by `comms.bus.communicating`:
a key outside the contract is dropped before it reaches the state and a
`violation` message records the attempt. That is how the brief's role
constraints hold structurally rather than by good intentions:

- Review cannot write `lines` or `provenance`: it can only send work back.
- Only Intake writes `messages` (the taxpayer conversation).
- Only Computation writes `lines` and `provenance`.
- Only Extraction writes `extracted`.

The read contract is what the orchestrator lists in its `request` so the
reader of the log can see what context each handoff carried.
"""
from __future__ import annotations

from typing import Any

COMMON_WRITES = frozenset({"handoffs", "events", "token_log", "mailbox", "hitl_log"})

WRITES: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"next_agent", "computation_version", "status", "loop_guard_tripped", "escalations", "extraction_done", "revise_target", "blocking_review"}),
    "intake": frozenset({"intake", "intake_complete", "intake_cursor", "messages", "escalations", "documents_in"}),
    "extraction": frozenset({"extracted", "extraction_done", "recompute_stage"}),
    "eligibility": frozenset({"eligibility", "escalations", "recompute_stage", "human_edit"}),
    "computation": frozenset({"lines", "provenance", "computation_version", "computation_notes", "escalations", "recompute_stage", "human_edit"}),
    "review": frozenset({"review_history", "review_round", "review_passed", "revise_target", "revise_reasons", "recompute_stage", "blocking_review"}),
    "escalation": frozenset({"escalations", "pending_escalation_id", "intake", "eligibility", "review_passed", "revise_target", "halted_reason", "status", "loop_guard_tripped", "review_round", "lines", "provenance", "provenance_seal", "blocking_review", "human_edit", "recompute_stage", "resolution_request", "document_overrides"}),
    "resolution_check": frozenset({"resolution_request", "escalations", "blocking_review", "loop_guard_tripped"}),
    "finalize": frozenset({"status", "halted_reason", "provenance_seal"}),  # the seal is security/integrity.py's, written as the draft completes
}

READS: dict[str, tuple[str, ...]] = {
    "intake": ("intake", "intake_cursor", "documents_in", "escalations", "messages"),
    "extraction": ("documents_in", "document_overrides"),
    "eligibility": ("intake", "extracted", "extraction_done", "escalations"),
    "computation": ("intake", "eligibility", "extracted", "computation_version"),
    "review": ("lines", "provenance", "eligibility", "extracted", "intake", "escalations", "review_round"),
    "escalation": ("escalations", "intake", "intake_complete"),
    "resolution_check": ("resolution_request", "escalations", "intake", "eligibility", "extracted", "blocking_review", "human_edit"),
    "finalize": ("eligibility", "halted_reason", "loop_guard_tripped", "review_round"),
}

GOALS: dict[str, str] = {
    "intake": "ask the taxpayer the next question",
    "extraction": "turn each supplied document into a typed record",
    "eligibility": "decide eligibility, box count, chart, schedules and scope, with citations",
    "computation": "populate every derived line through tax_math",
    "review": "try to falsify the assembled return",
    "escalation": "pause for a person to resolve the open escalation",
    "resolution_check": "verify the human action with deterministic rules and record yes or no",
    "finalize": "close the return",
}


def enforce_write_contract(agent: str, update: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split an update into the keys the agent may write and the ones it may not."""
    allowed = WRITES.get(agent, frozenset()) | COMMON_WRITES
    illegal = {k for k in update if k not in allowed}
    if agent == "escalation":
        # Human editing may invalidate derived records, never supply replacement figures.
        illegal.update(k for k in ("lines", "provenance") if k in update and update[k] != {})
    clean = {k: v for k, v in update.items() if k not in illegal}
    return clean, sorted(illegal)
