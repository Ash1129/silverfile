"""Human-in-the-loop escalation: pause with a LangGraph interrupt until a
Kushim employee resolves the open escalation in the dashboard (or CLI).

Resolutions:
  {"action": "continue", "note": "...", "facts_patch": {...}, "resolved_by": "..."}
      the return goes on, with any facts the employee corrected (for example a
      decided filing status, or a document conflict settled).
  {"action": "halt", "note": "...", "resolved_by": "..."}
      the employee takes the return over; the draft (if any) is kept as is.
  {"action": "accept", ...}  (review_unresolved only) accept the draft with the
      findings noted for the preparer.
"""
from __future__ import annotations

import json
from typing import Any

from langgraph.types import interrupt

from ..provenance import now_iso
from ..state import ReturnState


def escalation_node(state: ReturnState) -> dict[str, Any]:
    open_ = [e for e in state.get("escalations", []) if e.get("status") == "open"]
    if not open_:
        return {"handoffs": ["escalation"], "events": ["escalation: nothing open"]}
    esc = open_[0]
    resolution = interrupt({"type": "escalation", "escalation": esc, "return_id": state.get("return_id"), "open_count": len(open_), "summary": _summary(state)})
    if not isinstance(resolution, dict):
        resolution = {"action": "halt", "note": str(resolution)}
    action = resolution.get("action", "halt")
    resolved = {**esc, "status": "resolved", "resolution": resolution, "resolved_by": resolution.get("resolved_by", "kushim-employee"), "resolved_at": now_iso()}
    escalations = [resolved]  # same id: the reducer replaces the open entry in place
    out: dict[str, Any] = {"escalations": escalations, "pending_escalation_id": None, "handoffs": ["escalation"], "events": [f"escalation {esc['kind']} resolved: {action}"]}
    patch = resolution.get("facts_patch") or {}
    if patch:
        facts = json.loads(json.dumps(state.get("intake") or {}))
        for k, v in patch.items():
            if k == "taxpayer" and isinstance(v, dict):
                facts.setdefault("taxpayer", {}).update(v)
            elif k == "spouse" and isinstance(v, dict):
                facts["spouse"] = {**(facts.get("spouse") or {}), **v}
            else:
                facts[k] = v
        if "filing_status" in patch:
            facts.pop("_filing_status_unsure", None)
        out["intake"] = facts
        out["eligibility"] = {}  # stale: eligibility must run again
        out["review_passed"] = False
        out["revise_target"] = "eligibility" if state.get("intake_complete") else None
    if action == "halt":
        out["halted_reason"] = f"{esc['kind']}: {resolution.get('note') or 'taken over by a Kushim employee'}"
        out["status"] = "halted"
    elif action == "accept":
        out["review_passed"] = True
        out["revise_target"] = None
    elif action == "continue" and esc.get("kind") in ("loop_guard",):
        out["loop_guard_tripped"] = False
        out["handoffs"] = ["escalation", "reset"]
    elif action == "continue" and esc.get("kind") in ("out_of_scope", "r_box2b", "eligibility", "chart_not_applicable") and not patch:
        # a person decided these are fine to proceed with (for example the out-of-scope document was removed)
        out["eligibility"] = {}
        out["revise_target"] = "eligibility"
        out["review_passed"] = False
    elif action == "continue" and esc.get("kind") == "review_unresolved":
        out["review_round"] = 0
        out["review_passed"] = False
        out["revise_target"] = "computation"
    return out


def _summary(state: ReturnState) -> str:
    facts = state.get("intake") or {}
    tp = facts.get("taxpayer") or {}
    return f"{tp.get('first_name', '')} {tp.get('last_name', '')}".strip() + f" · status {facts.get('filing_status')} · {len(state.get('extracted') or {})} document(s) · lines computed: {bool(state.get('lines'))}"
