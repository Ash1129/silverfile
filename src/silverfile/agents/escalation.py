"""Checkpointed human review, explicit edit completion and deterministic verification."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from langgraph.types import interrupt

from .. import memory
from ..provenance import now_iso
from ..security import pii, validate
from ..state import ReturnState

HUMAN_ACTIONS = ("continue", "halt", "accept", "start_edit", "save_edit", "complete_edit", "resolve_review", "save_intervention_note", "append_intervention_note")


def requirement(code: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "description": description, "status": "pending", **extra}


def log_entry(event: str, *, result: str | None = None, **detail: Any) -> dict[str, Any]:
    """Structured trace, shared by checkpoints, the dashboard and the security log."""
    return pii.redact_deep({"event": event, "at": now_iso(), **({"result": result} if result else {}), **detail})


def revision(state: ReturnState) -> str:
    data = {k: state.get(k) for k in ("intake", "extracted", "lines", "provenance", "computation_version")}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def new_blocking_review(state: ReturnState, escalation_id: str) -> dict[str, Any]:
    actions = [requirement("recompute", "Complete human editing, then recompute and recheck the draft.")]
    latest = (state.get("review_history") or [{}])[-1]
    for f in latest.get("findings") or []:
        if f.get("severity") in ("high", "medium"):
            actions.append(requirement(f"finding:{f.get('check')}:{f.get('line')}", f.get("detail") or "Resolve this review finding.", check=f.get("check"), line=f.get("line")))
    return {"id": f"blocking:{escalation_id}", "escalation_id": escalation_id, "status": "open", "required_actions": actions}


def accept_requirements(state: ReturnState, *, require_note: bool = True) -> list[dict[str, Any]]:
    event = state.get("blocking_review") or {}
    todo = []
    if not event:
        todo.append(requirement("blocking_review_required", "Accept is available only for an explicit blocking review event."))
    else:
        todo.extend(a for a in event.get("required_actions", []) if a.get("status") != "completed")
        if event.get("status") != "resolved":
            todo.append(requirement("resolve_blocking_review", "Explicitly resolve the blocking review event after all required actions pass."))
        elif event.get("resolved_revision") != revision(state):
            todo.append(requirement("recheck_changed_draft", "The draft changed after the event was resolved; complete editing and resolve the event again."))
    if (state.get("human_edit") or {}).get("status") == "editing" or state.get("recompute_stage"):
        todo.append(requirement("complete_human_edit", "Finish human editing and wait for recomputation."))
    for e in state.get("escalations") or []:
        if e.get("status") == "open" and e.get("id") != event.get("escalation_id"):
            todo.append(requirement("resolve_other_escalation", f"Resolve {e.get('kind')} before accepting.", escalation_id=e.get("id")))
    saved_note = event.get("intervention_note") or ""
    if require_note and not saved_note.strip():
        todo.append(requirement("save_intervention_note", "Save a human intervention explanation to the workflow log before Accept. There is no application length limit."))
    elif require_note and event.get("note_revision") != revision(state):
        todo.append(requirement("save_current_explanation", "Save the explanation for the current verified draft."))
    return todo


def _patched_facts(facts: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    facts = json.loads(json.dumps(facts))
    for key, value in patch.items():
        if key in ("taxpayer", "spouse") and isinstance(value, dict):
            facts[key] = {**(facts.get(key) or {}), **value}
        else:
            facts[key] = value
    def confirmed(path: str) -> bool:
        parts = path.split(".")
        value: Any = patch
        for part in parts:
            if not isinstance(value, dict) or part not in value:
                return False
            value = value[part]
        return value is not None
    facts["_unknown_fields"] = [k for k in facts.get("_unknown_fields", []) if not confirmed(k)]
    if patch.get("filing_status"):
        facts.pop("_filing_status_unsure", None)
    return facts


def _merge_patch(previous: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(previous))
    for key, value in patch.items():
        merged[key] = {**(merged.get(key) or {}), **value} if key in ("taxpayer", "spouse") and isinstance(value, dict) else value
    return merged


def _resolved(esc: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    return {**esc, "status": "resolved", "resolution": resolution, "resolved_by": resolution.get("resolved_by", "local-reviewer"), "resolved_at": now_iso()}


def escalation_node(state: ReturnState) -> dict[str, Any]:
    open_events = [e for e in state.get("escalations", []) if e.get("status") == "open"]
    if not open_events:
        return {"handoffs": ["escalation"], "events": ["escalation: nothing open"]}
    esc = open_events[0]
    event = state.get("blocking_review")
    if esc.get("kind") == "review_unresolved" and (not event or event.get("escalation_id") != esc["id"]):
        event = new_blocking_review(state, esc["id"])
    view = {**state, "blocking_review": event}
    payload = interrupt({"type": "escalation", "escalation": esc, "return_id": state.get("return_id"), "open_count": len(open_events),
                         "blocking_review": event, "human_edit": state.get("human_edit"), "required_actions": accept_requirements(view),
                         "last_check": (state.get("hitl_log") or [None])[-1], "summary": _summary(state)})
    if not isinstance(payload, dict):
        payload = {"action": "halt", "note": str(payload)}
    action = payload.get("action", "halt")
    note = payload.get("note", "")
    out: dict[str, Any] = {"blocking_review": event, "handoffs": ["escalation"], "events": [], "hitl_log": []}
    def blocked(todo: list[dict[str, Any]]) -> dict[str, Any]:
        out["hitl_log"].append(log_entry("human_action_blocked", result="no", action=action, escalation_id=esc["id"], required_actions=todo))
        out["events"].append(f"human action {action} blocked; required actions are in hitl_log")
        return out
    if action not in HUMAN_ACTIONS or not isinstance(note, str):
        return blocked([requirement("valid_human_action", "Submit a supported action and a plain-text note.")])
    chunk_text = note
    note = pii.redact(note.replace("\r\n", "\n").replace("\r", "\n")).strip()
    try:
        patch = validate.clean_facts_patch(payload.get("facts_patch") or {})
    except ValueError as exc:
        return blocked([requirement("valid_facts_patch", str(exc))])
    actor = payload.get("resolved_by") or "local-reviewer"
    resolution = {"action": action, "note": note, "resolved_by": actor, "resolved_by_role": payload.get("resolved_by_role", "local")}
    if patch:
        resolution["facts_patch"] = patch
    if action == "halt":
        out.update(escalations=[_resolved(esc, resolution)], status="halted", halted_reason=f"{esc['kind']}: {note or 'taken over by a Kushim employee'}", resolution_request=None, recompute_stage=None)
        out["hitl_log"].append(log_entry("human_handoff", result="yes", escalation_id=esc["id"], actor=actor, note=note))
        return out
    if action in ("save_intervention_note", "append_intervention_note"):
        todo = accept_requirements(view, require_note=False)
        if action == "save_intervention_note" and not note.strip():
            todo.append(requirement("nonempty_intervention_note", "Write the human intervention explanation before saving it."))
        if patch:
            todo.append(requirement("complete_edit_first", "Save the explanation separately from fact corrections."))
        if todo:
            return blocked(todo)
        if action == "append_intervention_note":
            transfer = payload.get("note_transfer_id")
            index = payload.get("note_chunk_index")
            final = payload.get("note_final_chunk")
            if not isinstance(transfer, str) or len(transfer) != 32 or any(c not in "0123456789abcdef" for c in transfer) or type(index) is not int or index < 0 or type(final) is not bool or len(chunk_text.encode("utf-8")) > 131072:
                return blocked([requirement("valid_note_chunk", "Send an ordered explanation chunk of at most 128 KiB.")])
            draft = {"transfer_id": transfer, "next_chunk": 0, "text": "", "revision": revision(state)} if index == 0 else (event.get("note_draft") or {})
            if draft.get("transfer_id") != transfer or draft.get("next_chunk") != index or draft.get("revision") != revision(state):
                return blocked([requirement("ordered_note_chunks", "Restart the explanation upload; a chunk is missing, stale or out of order.")])
            text = pii.redact(draft["text"] + chunk_text)
            if not final:
                out["blocking_review"] = {**event, "intervention_note": "", "note_draft": {**draft, "next_chunk": index + 1, "text": text}}
                out["hitl_log"].append(log_entry("intervention_note_chunk_saved", result="yes", blocking_review_id=event["id"], transfer_id=transfer, chunk_index=index, stored_characters=len(text), actor=actor))
                return out
            note = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not note:
                return blocked([requirement("nonempty_intervention_note", "The complete explanation must contain text.")])
        out["blocking_review"] = {**event, "intervention_note": note, "note_draft": None, "note_revision": revision(state), "note_saved_by": actor, "note_saved_at": now_iso()}
        out["hitl_log"].append(log_entry("intervention_note_recorded", result="yes", escalation_id=esc["id"], blocking_review_id=event["id"], actor=actor, intervention_note=note))
        return out
    if action == "accept":
        todo = accept_requirements(view)
        saved_note = (event or {}).get("intervention_note") or ""
        if note.strip() and note != saved_note:
            todo.append(requirement("save_modified_explanation", "Save the changed explanation to the workflow log before Accept."))
        if esc.get("kind") != "review_unresolved":
            todo.append(requirement("accept_review_only", "This escalation cannot be accepted; correct its cause or hand it to a preparer."))
        if patch:
            todo.append(requirement("complete_edit_first", "Complete the fact edit separately before accepting."))
        if todo:
            return blocked(todo)
        note = saved_note
        resolution["note"] = note
        event = {**event, "accepted_at": now_iso(), "accepted_by": actor}
        out.update(blocking_review=event, escalations=[_resolved(esc, resolution)], review_passed=True, revise_target=None)
        out["hitl_log"].append(log_entry("review_accepted", result="yes", escalation_id=esc["id"], blocking_review_id=event["id"], actor=actor, intervention_note=note, required_actions=event["required_actions"]))
        return out
    if action in ("start_edit", "save_edit"):
        previous = state.get("human_edit") or {}
        if previous.get("status") != "editing" or previous.get("escalation_id") != esc["id"]:
            previous = {}
        out["human_edit"] = {"id": previous.get("id") if previous.get("status") == "editing" else f"edit:{esc['id']}:{len(state.get('hitl_log') or [])}",
                             "escalation_id": esc["id"], "status": "editing", "facts_patch": _merge_patch(previous.get("facts_patch") or {}, patch), "started_by": actor}
        out["hitl_log"].append(log_entry("human_edit_saved", action=action, actor=actor, escalation_id=esc["id"], changed_fields=sorted(out["human_edit"]["facts_patch"])))
        if event:
            out["blocking_review"] = {**event, "status": "open", "intervention_note": "", "note_draft": None, "required_actions": [{**a, "status": "pending"} for a in event.get("required_actions", [])]}
        return out
    if action == "resolve_review" and patch:
        return blocked([requirement("complete_edit_first", "Complete the fact correction before resolving the blocking review event.")])
    if action == "complete_edit" or (action == "continue" and (patch or (state.get("human_edit") or {}).get("status") == "editing")):
        previous = state.get("human_edit") or {}
        if previous.get("status") != "editing" or previous.get("escalation_id") != esc["id"]:
            previous = {}
        patch = _merge_patch(previous.get("facts_patch") or {}, patch)
        resolution["facts_patch"] = patch
        # the taxpayer's earlier answer stays in the answer history beside the employee's correction
        out["intake"] = memory.record_answer_changes(state.get("intake") or {}, _patched_facts(state.get("intake") or {}, patch), slot=esc["kind"], at=now_iso(), how="kushim")
        out.update(lines={}, provenance={}, provenance_seal=None, eligibility={}, review_passed=False, review_round=0, revise_target=None)
        out["human_edit"] = {"id": previous.get("id") or f"edit:{esc['id']}:{len(state.get('hitl_log') or [])}", "escalation_id": esc["id"], "status": "completed", "facts_patch": patch, "completed_by": actor, "completed_at": now_iso()}
        if event:
            out["blocking_review"] = {**event, "status": "open", "intervention_note": "", "note_draft": None, "required_actions": [{**a, "status": "pending"} for a in event.get("required_actions", [])]}
        out["recompute_stage"] = "eligibility" if state.get("intake_complete") and state.get("extraction_done") else None
        out["hitl_log"].append(log_entry("human_edit_completed", actor=actor, escalation_id=esc["id"], changed_fields=sorted(patch), previous_computation_version=state.get("computation_version", 0), recomputation="queued" if out["recompute_stage"] else "after_intake"))
    out["resolution_request"] = {"escalation_id": esc["id"], "kind": esc["kind"], "action": action, "resolution": resolution}
    return out


def _safety_requirements(state: ReturnState) -> list[dict[str, Any]]:
    from .eligibility import run_eligibility
    todo = validate.unknown_fact_actions(state)
    if not state.get("intake_complete") or not state.get("extraction_done"):
        return todo
    el = run_eligibility(state)["eligibility"]
    for key, text in (("eligible_1040sr", "Confirm eligibility for Form 1040-SR."), ("in_scope", "Remove the actual out-of-scope cause or hand the return to a preparer."), ("chart_applies", "Resolve the deduction-chart exception."), ("filing_status_valid", "Have a preparer decide a valid filing status.")):
        if not el.get(key):
            todo.append(requirement(key, text))
    for text in el.get("conflicts") or []:
        todo.append(requirement("correct_conflict", text))
    for doc_id, doc in (state.get("extracted") or {}).items():
        if doc.get("kind") == "1099-R" and (doc.get("fields") or {}).get("taxable_not_determined") is True:
            todo.append(requirement("correct_box2b_or_handoff", "Confirm a misread box 2b from evidence, or hand this return to a preparer.", document_id=doc_id))
    return todo


def resolution_check_node(state: ReturnState) -> dict[str, Any]:
    """A code-only checker: every Continue has exactly a yes/no outcome."""
    request = state.get("resolution_request") or {}
    esc = next((e for e in state.get("escalations") or [] if e.get("id") == request.get("escalation_id")), None)
    if not esc:
        return {"resolution_request": None, "handoffs": ["resolution_check"], "hitl_log": [log_entry("resolution_check", result="no", required_actions=[requirement("current_escalation", "Reload the current escalation.")])]}
    kind, action = esc["kind"], request["action"]
    facts = state.get("intake") or {}
    todo = _safety_requirements(state)
    if kind == "filing_status" and facts.get("filing_status") not in validate.FILING_STATUSES:
        todo.append(requirement("decide_filing_status", "A preparer must enter the filing status."))
    if kind == "exploitation":
        if facts.get("answering_party") == "third_party_no_authority":
            todo.append(requirement("verify_authority", "Verify authority and correct the answering-party facts."))
        if facts.get("refund_account_is_own") is False:
            todo.append(requirement("verify_refund_account", "Verify and correct the refund account ownership facts."))
    if kind == "conflict" and "could not answer" in esc.get("reason", "").lower():
        from .intake import next_slot
        if next_slot(facts) == state.get("intake_cursor"):
            todo.append(requirement("confirm_required_answer", "Confirm the required answer that paused intake."))
    if kind == "loop_guard" and (state.get("fault_injection") or {}).get("ping_pong"):
        todo.append(requirement("remove_loop_cause", "Correct the repeating handoff cause before continuing."))
    event = state.get("blocking_review") or {}
    if kind == "review_unresolved" or action == "resolve_review":
        from .review import rule_checks
        candidate = {**state, "escalations": [e for e in state.get("escalations", []) if e.get("id") != esc["id"]]}
        findings, _ = rule_checks(candidate)
        todo.extend(requirement(f"finding:{f.get('check')}:{f.get('line')}", f["detail"]) for f in findings if f.get("severity") in ("high", "medium"))
        todo.extend(a for a in event.get("required_actions", []) if a.get("status") != "completed")
        if not event or not state.get("review_passed"):
            todo.append(requirement("pass_recheck", "Complete editing and obtain a passing recheck before resolving the event."))
        if action == "resolve_review" and kind != "review_unresolved":
            todo.append(requirement("review_event_only", "Only a blocking review event can use this action."))
    result = "no" if todo else "yes"
    resolution = request["resolution"]
    out: dict[str, Any] = {"resolution_request": None, "handoffs": ["resolution_check"], "events": [f"{action} verification: {result}"],
                           "hitl_log": [log_entry("continue_check" if action == "continue" else "resolution_check", result=result, action=action, escalation_id=esc["id"], actor=resolution.get("resolved_by"), checker="deterministic_rules", required_actions=todo)]}
    if todo:
        return out
    if action == "resolve_review":
        event = {**event, "status": "resolved", "resolved_by": resolution["resolved_by"], "resolved_at": now_iso(), "resolved_revision": revision(state)}
        out["blocking_review"] = event
        out["hitl_log"].append(log_entry("blocking_review_resolved", result="yes", blocking_review_id=event["id"], actor=resolution["resolved_by"], required_actions=event["required_actions"]))
    elif kind != "review_unresolved":
        out["escalations"] = [_resolved(esc, resolution)]
        if kind == "loop_guard":
            out.update(loop_guard_tripped=False, handoffs=["resolution_check", "reset"])
    return out


def after_remediation_step(agent: str, state: ReturnState, update: dict[str, Any]) -> dict[str, Any]:
    """Advance the explicit recomputation state once per worker completion."""
    stage = state.get("recompute_stage")
    edit = state.get("human_edit") or {}
    if stage == "eligibility" and agent == "eligibility":
        todo = _safety_requirements({**state, **update})
        update["recompute_stage"] = None if todo else "computation"
        if todo:
            update["human_edit"] = {**edit, "status": "blocked"}
            update["hitl_log"] = [log_entry("recomputation_blocked", result="no", edit_id=edit.get("id"), required_actions=todo)]
    elif agent == "computation" and (stage == "computation" or edit.get("status") == "completed"):
        if update.get("lines"):
            update["human_edit"] = {**edit, "status": "recomputed", "computation_version": state.get("computation_version")}
            update["hitl_log"] = [log_entry("human_edit_recomputed", result="yes", edit_id=edit.get("id"), computation_version=state.get("computation_version"))]
            if stage:
                update["recompute_stage"] = "review"
        else:
            update["recompute_stage"] = None
    elif stage == "review" and agent == "review":
        update["recompute_stage"] = None
        event = state.get("blocking_review")
        if event:
            latest = update["review_history"][-1]
            remaining = {f"finding:{f.get('check')}:{f.get('line')}" for f in latest.get("findings", []) if f.get("severity") in ("high", "medium") and f.get("check") != "open-escalation"}
            actions = [{**a, "status": "pending" if a["code"] in remaining else "completed"} for a in event["required_actions"]]
            existing = {a["code"] for a in actions}
            actions.extend(requirement(f"finding:{f.get('check')}:{f.get('line')}", f["detail"], check=f.get("check"), line=f.get("line")) for f in latest.get("findings", []) if f.get("severity") in ("high", "medium") and f.get("check") != "open-escalation" and f"finding:{f.get('check')}:{f.get('line')}" not in existing)
            update["blocking_review"] = {**event, "status": "open", "required_actions": actions}
    return update


def _summary(state: ReturnState) -> str:
    facts = state.get("intake") or {}
    tp = facts.get("taxpayer") or {}
    return f"{tp.get('first_name', '')} {tp.get('last_name', '')}".strip() + f"; filing status {facts.get('filing_status')}; documents {len(state.get('extracted') or {})}"
