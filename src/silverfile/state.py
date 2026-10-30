"""Shared graph state for SilverFile, with the reducers that make parallel
writes safe. Every agent reads this and returns a partial update.

Design notes
- `extracted` uses `merge_documents`: extraction runs in parallel (one branch
  per document) and each branch writes its own record keyed by document id, so
  concurrent writes never clobber each other. A second write for the same id
  with a higher `version` wins; an equal version is kept and flagged.
- `mailbox` uses `merge_messages`: the inter-agent message bus, deduplicated
  by content-addressed id and ordered by a logical clock, so the parallel
  extraction branches can post to it at once.
- `handoffs`, `escalations`, `review_history`, `token_log`, `events` are
  append-only lists (operator.add).
- Everything else is last-writer-wins and is only written by one agent at a
  time by construction (the orchestrator routes to one agent, except the
  extraction fan-out).
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages

FilingStatus = Literal["single", "mfj", "mfs", "hoh", "qss"]
AgentName = Literal["orchestrator", "intake", "extraction", "eligibility", "computation", "review", "escalation", "resolution_check"]
ReturnStatus = Literal["new", "intake", "extracting", "eligibility", "computing", "review", "waiting_human", "complete", "halted"]


class Person(TypedDict, total=False):
    first_name: str
    last_name: str
    ssn: str  # 900-series test SSNs only; validated at entry
    dob: str  # ISO date
    blind: bool | None
    date_of_death: str | None


class Dependent(TypedDict, total=False):
    name: str
    ssn: str
    relationship: str


class IntakeFacts(TypedDict, total=False):
    answering_party: Literal["self", "third_party_with_authority", "third_party_no_authority", "unknown"]
    third_party_authority: str | None
    taxpayer: Person
    spouse: Person | None
    married: bool | None
    spouse_died_in_year: bool | None
    filing_status: FilingStatus | None
    dependents: list[Dependent] | None
    can_be_claimed_as_dependent: bool | None
    documents_declared: list[str] | None  # e.g. ["W-2", "SSA-1099", "1099-INT"]; None is unknown
    other_income_sources: list[str] | None  # free text; unknown is not an empty list
    estimated_payments: float | None
    schedule1_adjustments: float | None  # line 10 (Schedule 1, line 26)
    schedule1_other_income: float | None  # line 8
    schedule2_line3: float | None  # line 17
    schedule2_line21: float | None  # line 23
    schedule3_line8: float | None  # line 20
    refund_account_description: str | None
    refund_account_is_own: bool | None
    noted_questions: list[str]  # advice questions noted for the preparer
    volunteered_ssn_events: list[str]  # how an unrequested SSN was handled
    mfs_spouse_itemizes: bool | None
    dual_status_alien: bool | None
    document_claims: list[dict[str, Any]]  # source-linked statements, never replacement return lines


class DocumentInput(TypedDict, total=False):
    id: str
    kind: str  # "W-2" | "SSA-1099" | "1099-R" | "1099-INT" | "1099-DIV" | "unknown" | anything else (out of scope)
    path: str | None  # image or json file on disk
    payload: dict[str, Any] | None  # typed fields supplied directly (fixtures / dashboard form)
    declared_by: str  # "taxpayer" | "fixture" | "dashboard"
    uploaded_at: str
    media_type: str  # for uploads: the type identified from the file's bytes (security/uploads.py)
    original_name: str  # for uploads: the cleaned file name, a label only (the stored name is random)
    sha256: str
    size: int


class ExtractedDocument(TypedDict, total=False):
    id: str
    kind: str
    version: int
    fields: dict[str, Any]  # box label -> value, exactly as printed
    boxes: dict[str, str]  # field -> box reference on the form (e.g. "wages": "Box 1")
    confidence: float
    method: str  # "vision:claude-sonnet-5-5" | "payload" | "fixture"
    source_ref: str  # path or payload id
    issues: list[str]
    out_of_scope: bool
    extracted_at: str
    tokens_in: int
    tokens_out: int
    security_flags: list[str]  # prompt-injection text, an SSN in a text field, an implausible amount (security/validate.py)
    correction: dict[str, Any]
    superseded_by: str | None  # confirmed identical-source duplicate, retained for audit


class Provenance(TypedDict, total=False):
    line: str
    label: str
    amount: float
    source_kind: Literal["document", "computed", "user", "chart", "worksheet", "rate-schedule"]
    sources: list[str]  # document ids, line ids, or "intake"
    method: str  # tax_math tool name, or "direct user input"
    inputs: dict[str, Any]
    transport: str  # "mcp-stdio" | "direct" | "n/a"
    citations: list[str]  # reference corpus ids
    entered_by: str | None
    entered_at: str | None
    note: str | None


class EligibilityResult(TypedDict, total=False):
    eligible_1040sr: bool
    filing_status: FilingStatus | None
    filing_status_valid: bool
    boxes_12d: dict[str, bool]
    box_count: int
    chart_applies: bool | None  # None: required exception facts have not been confirmed
    schedules: list[str]
    in_scope: bool
    out_of_scope_items: list[str]
    conflicts: list[str]
    citations: list[str]
    determinations: list[dict[str, Any]]  # {claim, result, citation}
    escalate: list[dict[str, Any]]  # {kind, reason}


class Finding(TypedDict, total=False):
    id: str
    severity: Literal["high", "medium", "low"]
    line: str | None
    check: str
    detail: str
    send_to: Literal["computation", "extraction", "eligibility", "intake", "human"] | None
    fixable: bool


class ReviewRound(TypedDict, total=False):
    round: int
    passed: bool
    findings: list[Finding]
    recomputed_lines: dict[str, float]
    method: str  # "rules" | "rules+claude-sonnet-5-5"
    tokens_in: int
    tokens_out: int
    at: str


class Escalation(TypedDict, total=False):
    id: str
    kind: str  # out_of_scope | conflict | r_box2b | advice | filing_status | exploitation | loop_guard | review_unresolved | chart_not_applicable | eligibility | uncertain_fact
    reason: str
    raised_by: AgentName
    raised_at: str
    details: dict[str, Any]
    status: Literal["open", "resolved"]
    resolution: dict[str, Any] | None
    resolved_by: str | None
    resolved_at: str | None


class BlockingReviewEvent(TypedDict, total=False):
    id: str
    escalation_id: str
    status: Literal["open", "resolved"]
    required_actions: list[dict[str, Any]]
    resolved_by: str
    resolved_at: str
    resolved_revision: str
    intervention_note: str
    note_revision: str
    note_saved_by: str
    note_saved_at: str
    note_draft: dict[str, Any] | None
    accepted_at: str
    accepted_by: str


class HumanEdit(TypedDict, total=False):
    id: str
    escalation_id: str
    status: Literal["editing", "completed", "recomputed", "blocked"]
    facts_patch: dict[str, Any]
    started_by: str
    completed_by: str
    completed_at: str
    computation_version: int


class TokenEvent(TypedDict, total=False):
    agent: str
    model: str
    purpose: str
    tokens_in: int
    tokens_out: int
    at: str


class AgentMessage(TypedDict, total=False):
    """One envelope on the inter-agent message bus (see comms/protocol.py)."""
    id: str  # content-addressed: the same message delivered twice is stored once
    seq: int  # logical clock; parallel branches share a seq and are ordered by id
    thread: str  # correlation id: a request, its reply and any follow-ups share it
    in_reply_to: str | None
    sender: str
    recipient: str
    performative: str  # request | inform | revise | done | escalate | resolve | refuse | violation | ask | answer
    topic: str
    body: dict[str, Any]
    at: str


def merge_messages(left: list[AgentMessage] | None, right: list[AgentMessage] | None) -> list[AgentMessage]:
    """Order-independent append for the message bus (handles concurrent writes).

    Messages are deduplicated by id and kept sorted by (seq, id). Parallel
    extraction branches all write at the same seq, so whichever branch finishes
    first, the merged log is identical. A redelivered message (same id) is a
    no-op, which makes delivery idempotent across checkpoint replays.
    """
    by_id: dict[str, AgentMessage] = {m["id"]: m for m in (left or [])}
    for m in right or []:
        by_id.setdefault(m["id"], m)
    return sorted(by_id.values(), key=lambda m: (int(m.get("seq", 0)), m["id"]))


def merge_documents(left: dict[str, ExtractedDocument] | None, right: dict[str, ExtractedDocument] | None) -> dict[str, ExtractedDocument]:
    """Order-independent merge keyed by document id (handles concurrent writes).

    The record with the higher version wins. Two writes of the same version are
    kept as the later one plus an issue noting the duplicate, so nothing is
    dropped silently.
    """
    out: dict[str, ExtractedDocument] = dict(left or {})
    for k, v in (right or {}).items():
        if k not in out:
            out[k] = v
            continue
        cur = out[k]
        if int(v.get("version", 0)) > int(cur.get("version", 0)):
            out[k] = v
        elif int(v.get("version", 0)) == int(cur.get("version", 0)) and v is not cur:
            merged = dict(v)
            merged["issues"] = list(dict.fromkeys([*cur.get("issues", []), *v.get("issues", []), "duplicate write for the same document version was merged"]))
            out[k] = merged  # type: ignore[assignment]
    return out


def merge_escalations(left: list[Escalation] | None, right: list[Escalation] | None) -> list[Escalation]:
    """Append-only by id: a later entry with the same id (for example the resolved
    copy of an open escalation) replaces the earlier one in place, so the list
    reads as a history and `status == "open"` means open."""
    out: list[Escalation] = list(left or [])
    index = {e.get("id"): i for i, e in enumerate(out)}
    for e in right or []:
        if e.get("id") in index:
            out[index[e["id"]]] = e
        else:
            index[e.get("id")] = len(out)
            out.append(e)
    return out


def last_value(left: Any, right: Any) -> Any:
    return right if right is not None else left


class ReturnState(TypedDict, total=False):
    # identity
    return_id: str
    tax_year: int
    created_at: str
    status: ReturnStatus
    # intake
    messages: Annotated[list, add_messages]
    intake: IntakeFacts
    intake_complete: bool
    intake_cursor: str | None  # id of the slot being asked
    resume_summary: str | None
    # documents
    documents_in: list[DocumentInput]
    extracted: Annotated[dict[str, ExtractedDocument], merge_documents]
    extraction_done: bool
    # eligibility
    eligibility: EligibilityResult
    # computation
    lines: dict[str, float]
    provenance: dict[str, Provenance]
    computation_version: int
    computation_notes: list[str]
    # review
    review_history: Annotated[list[ReviewRound], operator.add]
    review_round: int
    review_passed: bool
    provenance_seal: dict[str, Any] | None  # hash chain over every line's provenance, signed (security/integrity.py)
    revise_target: str | None
    revise_reasons: list[str]
    # escalation / HITL
    escalations: Annotated[list[Escalation], merge_escalations]
    pending_escalation_id: str | None
    blocking_review: BlockingReviewEvent | None
    human_edit: HumanEdit | None
    recompute_stage: Literal["extraction", "eligibility", "computation", "review"] | None
    resolution_request: dict[str, Any] | None
    document_overrides: dict[str, dict[str, Any]]
    hitl_log: Annotated[list[dict[str, Any]], operator.add]
    halted_reason: str | None
    # orchestration
    handoffs: Annotated[list[str], operator.add]
    loop_guard_tripped: bool
    next_agent: str | None
    # inter-agent communication: every handoff, result, work order and escalation as a typed envelope
    mailbox: Annotated[list[AgentMessage], merge_messages]
    # memory / telemetry
    summary: str | None
    token_log: Annotated[list[TokenEvent], operator.add]
    events: Annotated[list[str], operator.add]
    # test hooks (never set in production paths)
    fault_injection: dict[str, Any]


MAX_REVIEW_ROUNDS = 3  # explicit, tested bound on the review -> revise cycle
MAX_HANDOFFS = 40  # absolute ceiling on agent handoffs per return
LOOP_PATTERN_REPEATS = 3  # the same A->B->A pattern this many times trips the loop guard
