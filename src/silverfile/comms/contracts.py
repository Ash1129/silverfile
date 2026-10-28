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

COMMON_WRITES = frozenset({"handoffs", "events", "token_log", "mailbox"})

WRITES: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"next_agent", "computation_version", "status", "loop_guard_tripped", "escalations", "extraction_done", "revise_target"}),
    "intake": frozenset({"intake", "intake_complete", "intake_cursor", "messages", "escalations", "documents_in"}),
    "extraction": frozenset({"extracted", "extraction_done"}),
    "eligibility": frozenset({"eligibility", "escalations"}),
    "computation": frozenset({"lines", "provenance", "computation_version", "computation_notes"}),
    "review": frozenset({"review_history", "review_round", "review_passed", "revise_target", "revise_reasons"}),
    "escalation": frozenset({"escalations", "pending_escalation_id", "intake", "eligibility", "review_passed", "revise_target", "halted_reason", "status", "loop_guard_tripped", "review_round"}),
    "finalize": frozenset({"status", "halted_reason"}),
}

READS: dict[str, tuple[str, ...]] = {
    "intake": ("intake", "intake_cursor", "documents_in", "escalations", "messages"),
    "extraction": ("documents_in",),
    "eligibility": ("intake", "extracted", "extraction_done", "escalations"),
    "computation": ("intake", "eligibility", "extracted", "computation_version"),
    "review": ("lines", "provenance", "eligibility", "extracted", "intake", "escalations", "review_round"),
    "escalation": ("escalations", "intake", "intake_complete"),
    "finalize": ("eligibility", "halted_reason", "loop_guard_tripped", "review_round"),
}

GOALS: dict[str, str] = {
    "intake": "ask the taxpayer the next question",
    "extraction": "turn each supplied document into a typed record",
    "eligibility": "decide eligibility, box count, chart, schedules and scope, with citations",
    "computation": "populate every derived line through tax_math",
    "review": "try to falsify the assembled return",
    "escalation": "pause for a person to resolve the open escalation",
    "finalize": "close the return",
}


def enforce_write_contract(agent: str, update: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split an update into the keys the agent may write and the ones it may not."""
    allowed = WRITES.get(agent, frozenset()) | COMMON_WRITES
    clean = {k: v for k, v in update.items() if k in allowed}
    return clean, sorted(k for k in update if k not in allowed)
