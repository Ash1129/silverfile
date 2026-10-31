"""Thin service layer shared by the dashboard and the CLI.

It drives the compiled graph (see graph.py) one return at a time and keeps a
small SQLite index at settings().index_db so the list page never has to load a
checkpoint. The index holds the taxpayer's name, status and a plain-language
summary only (name and summary encrypted); it never holds an SSN.

Contract with the graph
- create_return seeds a thread with new_return_state (update_state on a fresh
  thread is treated as START input); run() invokes until the first interrupt.
- pending() is the first interrupt payload: {"type": "question"|"upload"|
  "escalation", ...} or None when nothing is waiting.
- answer() resumes a question or upload interrupt; resolve() resumes an
  escalation interrupt. The index is refreshed after every invoke.

Security contract (docs/THREAT_MODEL.md)
- Every call that changes a return names its actor (security/auth.Principal)
  and is refused unless that actor may: a taxpayer only answers their own
  return's question, only staff with resolve_escalation resolve, and the
  resolver recorded is the authenticated actor, never a form field.
- A taxpayer's input reaches the graph only as the text (and checked uploads)
  of an answer to the question Intake asked: there is no path from a taxpayer
  to the orchestrator, to an escalation, or to a state update.
- A form names the interrupt it answers; an answer to a question that has
  already been answered (a replayed or double-submitted form) is refused.
- State loaded from a checkpoint is decrypted (authenticated) and then checked
  against the shapes the agents write (security/validate.check_state).
- Each invoke is diffed for security events (escalations raised and resolved,
  SSNs refused, flagged text or documents) and those go to the security log.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from langgraph.types import Command

from . import llm
from .config import ROOT, settings
from .forms.render import mask_ssn, mask_ssn_deep
from .graph import app, new_return_state
from .memory import session_summary, with_resume_recap
from .provenance import now_iso
from .security import audit, auth, crypto, pii, ratelimit, uploads, validate
from .security.auth import Principal
from .agents.escalation import HUMAN_ACTIONS

FIXTURES_DIR = ROOT / "data" / "fixtures"
DEMO_W2_IMAGE = ROOT / "demo" / "w2_image_1.jpg"
KIND_HINTS = (("1099int", "1099-INT"), ("1099div", "1099-DIV"), ("1099r", "1099-R"), ("ssa", "SSA-1099"), ("w2", "W-2"))
ACTIONS = HUMAN_ACTIONS
MAX_ANSWER_CHARS = 1000

_lock = threading.RLock()  # graph invokes are serialised per process (one SQLite checkpointer)


class ServiceError(ValueError):
    """A request that does not fit the return's current state (bad input, nothing pending)."""


class FactsPatchError(ServiceError):
    """An invalid facts patch, rejected before any workflow state is changed."""


class Forbidden(PermissionError):
    """The actor may not do this to this return."""


class StateRejected(RuntimeError):
    """A checkpoint that failed decryption or the schema check: tampered, replayed or corrupt."""


class Stale(ServiceError):
    """The form answers an interrupt that is no longer the one waiting (a replay or a double submit)."""


# ---------------------------------------------------------------- index
SCHEMA = """
CREATE TABLE IF NOT EXISTS returns (
  return_id        TEXT PRIMARY KEY,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  taxpayer_name    TEXT,
  status           TEXT NOT NULL,
  waiting_on_human INTEGER NOT NULL DEFAULT 0,
  summary          TEXT,
  pending_type     TEXT,
  fixture          TEXT
)
"""


def _index() -> sqlite3.Connection:
    path = settings().index_db
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    if fresh:
        crypto.restrict_file(path)
    return conn


def _graph():
    return app()


def _cfg(return_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": return_id}}


def _interrupts(snap) -> list[Any]:
    return [i.value for t in snap.tasks for i in t.interrupts]


def _interrupt_ids(snap) -> list[str]:
    return [i.id for t in snap.tasks for i in t.interrupts]


def _snapshot(return_id: str):
    try:
        snap = _graph().get_state(_cfg(return_id))
    except crypto.Undecryptable as e:
        audit.record("state_rejected", return_id=return_id, reason="checkpoint failed decryption")
        raise StateRejected("This return's saved state failed its integrity check and was not loaded.") from e
    if not snap.values:
        raise KeyError(return_id)
    problems = validate.check_state(snap.values, return_id)
    if problems:
        audit.record("state_rejected", return_id=return_id, problems=problems[:10])
        raise StateRejected("This return's saved state does not match the expected schema and was not loaded: " + "; ".join(problems[:3]))
    return snap


def taxpayer_name(state: dict[str, Any]) -> str:
    tp = (state.get("intake") or {}).get("taxpayer") or {}
    return f"{tp.get('first_name', '')} {tp.get('last_name', '')}".strip()


def _refresh(return_id: str, fixture: str | None = None) -> dict[str, Any]:
    """Re-read the thread and upsert its index row. Returns the state values."""
    snap = _snapshot(return_id)
    values = snap.values
    pend = _interrupts(snap)
    first = pend[0] if pend else None
    pending_type = first.get("type") if isinstance(first, dict) else None
    status = values.get("status") or "new"
    waiting = int(status == "waiting_human" or pending_type == "escalation")
    try:
        summary = session_summary(values)
    except Exception:  # the summary is a nicety; never fail a refresh on it
        summary = ""
    now = now_iso()
    with _index() as conn:
        conn.execute(
            """INSERT INTO returns (return_id, created_at, updated_at, taxpayer_name, status, waiting_on_human, summary, pending_type, fixture)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(return_id) DO UPDATE SET updated_at=excluded.updated_at, taxpayer_name=excluded.taxpayer_name, status=excluded.status,
                 waiting_on_human=excluded.waiting_on_human, summary=excluded.summary, pending_type=excluded.pending_type,
                 fixture=COALESCE(excluded.fixture, returns.fixture)""",
            (return_id, values.get("created_at") or now, now, crypto.encrypt_text(mask_ssn(taxpayer_name(values))), status, waiting, crypto.encrypt_text(mask_ssn(summary)), pending_type, fixture),
        )
    return values


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["taxpayer_name"] = crypto.decrypt_text(d.get("taxpayer_name"))
    d["summary"] = crypto.decrypt_text(d.get("summary"))
    return d


# ---------------------------------------------------------------- invoking, with the security diff
def _invoke(return_id: str, inp: Any, actor: Principal, action: str) -> dict[str, Any]:
    """One graph invoke charged to this return's model budget, diffed for security events."""
    before = dict(_snapshot(return_id).values)
    token = llm.current_return.set(return_id)
    try:
        _graph().invoke(inp, _cfg(return_id))
    finally:
        llm.current_return.reset(token)
    after = _refresh(return_id)
    _audit_diff(return_id, before, after, actor, action)
    return after


def _audit_diff(rid: str, before: dict[str, Any], after: dict[str, Any], actor: Principal, action: str) -> None:
    for entry in (after.get("hitl_log") or [])[len(before.get("hitl_log") or []):]:
        detail = {k: v for k, v in entry.items() if k not in {"actor", "return_id"}}
        audit.record("hitl_workflow", actor=actor.label, return_id=rid, workflow_actor=entry.get("actor"), **detail)
    old = {e.get("id"): e for e in before.get("escalations") or []}
    for e in after.get("escalations") or []:
        prev = old.get(e.get("id"))
        if prev is None:
            audit.record("escalation_raised", actor=actor.label, return_id=rid, escalation=e.get("id"), kind=e.get("kind"), raised_by=e.get("raised_by"))
        if e.get("status") == "resolved" and (prev is None or prev.get("status") != "resolved"):
            if action != "resolve":
                # the escalation trigger cannot be cleared by anything but a staff resolution; if it ever is, say so loudly
                audit.record("invariant_violation", actor=actor.label, return_id=rid, escalation=e.get("id"), action=action)
                raise StateRejected("An escalation was resolved outside a staff resolution; the return was stopped for review.")
            res = e.get("resolution") or {}
            audit.record("escalation_resolved", actor=actor.label, return_id=rid, escalation=e.get("id"), kind=e.get("kind"),
                         decision=res.get("action"), note=res.get("note"), facts_patched=sorted((res.get("facts_patch") or {}).keys()), resolved_at=e.get("resolved_at"))
    bf, af = before.get("intake") or {}, after.get("intake") or {}
    for ev in (af.get("volunteered_ssn_events") or [])[len(bf.get("volunteered_ssn_events") or []):]:
        audit.record("ssn_rejected" if ("non-test" in ev or "NOT a test" in ev) else "ssn_volunteered", actor=actor.label, return_id=rid, event=ev)
    for ev in (af.get("security_flags") or [])[len(bf.get("security_flags") or []):]:
        audit.record("prompt_injection_suspected", actor=actor.label, return_id=rid, source="answer", event=ev)
    old_docs = before.get("extracted") or {}
    for doc_id, d in (after.get("extracted") or {}).items():
        flags = d.get("security_flags") or []
        if flags and flags != ((old_docs.get(doc_id) or {}).get("security_flags") or []):
            audit.record("document_flagged", actor=actor.label, return_id=rid, document=doc_id, flags=flags[:5])
    if after.get("status") != before.get("status") and after.get("status") in ("complete", "halted"):
        audit.record(f"return_{after['status']}", actor=actor.label, return_id=rid, sealed=bool(after.get("provenance_seal")), reason=after.get("halted_reason"))


def _require(actor: Principal, return_id: str, permission: str | None = None) -> None:
    if not isinstance(actor, Principal) or not actor.may_access(return_id) or (permission and not actor.can(permission)):
        audit.record("access_denied", actor=getattr(actor, "label", None), return_id=return_id, permission=permission)
        raise Forbidden("You are not allowed to do that on this return.")


# ---------------------------------------------------------------- lifecycle
def new_return_id() -> str:
    """An unguessable id: 128 random bits (UUID4), never a counter."""
    return "ret-" + uuid.uuid4().hex


def create_return(documents: list[dict[str, Any]] | None = None, intake: dict[str, Any] | None = None, fault_injection: dict[str, Any] | None = None, *, return_id: str | None = None, fixture: str | None = None, actor: Principal | None = None) -> str:
    """Seed a new thread (nothing runs yet) and index it. Call run() to start it."""
    if fault_injection and not validate.faults_allowed():
        raise Forbidden("Fault injection is a test hook and is off in this environment.")
    if actor is not None and not actor.can("create_return"):
        raise Forbidden("You are not allowed to start returns.")
    rid = return_id or new_return_id()
    state = new_return_state(rid, documents=documents, fault_injection=fault_injection, intake=intake)
    with _lock:
        if _graph().get_state(_cfg(rid)).values:
            raise ServiceError("A return with that id already exists.")
        _graph().update_state(_cfg(rid), state)
        _refresh(rid, fixture=fixture)
    audit.record("return_created", actor=actor.label if actor else "system", return_id=rid, fixture=fixture, prefilled=bool(intake), documents=len(documents or []))
    return rid


def run(return_id: str, actor: Principal | None = None) -> dict[str, Any]:
    """Start or advance the return: invoke the graph until it finishes or pauses.

    It never resumes an interrupt (that takes an answer or a resolution), so it
    cannot be used to step past a question or an escalation.
    """
    actor = actor or auth.system()
    _require(actor, return_id, "advance_return")
    with _lock:
        snap = _snapshot(return_id)
        if snap.next and not _interrupts(snap):
            return _invoke(return_id, None, actor, "run")
        return _refresh(return_id)


def pending(return_id: str) -> dict[str, Any] | None:
    """The first interrupt payload waiting on a person, or None."""
    pend = _interrupts(_snapshot(return_id))
    return pend[0] if pend else None


def pending_for_taxpayer(return_id: str) -> dict[str, Any] | None:
    """pending(), as the taxpayer sees it: with a recap in front after a break (memory.with_resume_recap)."""
    snap = _snapshot(return_id)
    pend = _interrupts(snap)
    return with_resume_recap(pend[0], snap.values) if pend else None


def pending_id(return_id: str) -> str | None:
    """The id of the interrupt pending(); forms carry it so a replay is refused."""
    ids = _interrupt_ids(_snapshot(return_id))
    return ids[0] if ids else None


def _check_interrupt(return_id: str, interrupt_id: str | None) -> None:
    if interrupt_id is None:
        return
    current = pending_id(return_id)
    if not current or not secrets_equal(current, interrupt_id):
        audit.record("stale_submission", return_id=return_id)
        raise Stale("That form was for a question that has already been answered. The page has the current one.")


def secrets_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(str(a), str(b))


def clean_answer(text: Any) -> str:
    """A taxpayer's answer as plain text: a string, no control characters, at most MAX_ANSWER_CHARS."""
    if not isinstance(text, str):
        raise ServiceError("An answer is plain text.")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()
    return text[:MAX_ANSWER_CHARS]


def answer(return_id: str, text: str, documents: list[dict[str, Any]] | None = None, *, actor: Principal, interrupt_id: str | None = None) -> dict[str, Any]:
    """Answer the pending question. At the upload step, `documents` are appended to the return.

    The only door from a taxpayer into the graph: the resume value is built here,
    from text and documents this server produced, never passed through from a request.
    """
    _require(actor, return_id)
    if actor.kind == "staff":
        # role separation: a preparer answering for the taxpayer is a third party answering
        audit.record("access_denied", actor=actor.label, return_id=return_id, permission="answer_as_taxpayer")
        raise Forbidden("Staff do not answer for the taxpayer; the taxpayer answers in their own session.")
    text = clean_answer(text)
    with _lock:
        _check_interrupt(return_id, interrupt_id)
        p = pending(return_id)
        if not p or p.get("type") not in ("question", "upload"):
            raise ServiceError("No question is waiting for the taxpayer on this return.")
        docs = [_trusted_document(d) for d in (documents or [])]
        if docs and p.get("type") != "upload":
            raise ServiceError("Documents can only be attached at the upload step of the conversation.")
        resume: Any = {"text": text, "documents": docs} if p.get("type") == "upload" else text
        return _invoke(return_id, Command(resume=resume), actor, "answer")


DOCUMENT_KEYS = {"id", "kind", "path", "payload", "declared_by", "uploaded_at", "media_type", "original_name", "sha256", "size"}


def _trusted_document(d: dict[str, Any]) -> dict[str, Any]:
    """A DocumentInput this server built (an upload it stored, or a fixture), or ServiceError."""
    if not isinstance(d, dict) or set(d) - DOCUMENT_KEYS:
        raise ServiceError("Malformed document.")
    path = Path(str(d.get("path") or ""))
    roots = (settings().uploads_dir.resolve(), FIXTURES_DIR.resolve(), DEMO_W2_IMAGE.parent.resolve())
    if d.get("path") and not any(path.resolve().is_relative_to(r) for r in roots):
        raise ServiceError("Documents must be uploaded through SilverFile.")
    return d


def resolve(return_id: str, resolution: dict[str, Any], *, actor: Principal, interrupt_id: str | None = None) -> dict[str, Any]:
    """Resolve the open escalation: {"action": continue|halt|accept, "note", "facts_patch"}.

    Only staff with resolve_escalation (or the system, when seeding). The
    resolver, their role and the time are recorded from the session, not the form.
    """
    _require(actor, return_id, "resolve_escalation")
    with _lock:
        _check_interrupt(return_id, interrupt_id)
        p = pending(return_id)
        if not p or p.get("type") != "escalation":
            raise ServiceError("No escalation is waiting on this return.")
        action = (resolution or {}).get("action")
        if action not in ACTIONS:
            raise ServiceError(f"action must be one of {', '.join(ACTIONS)}.")
        note = (resolution or {}).get("note") or ""
        if not isinstance(note, str):
            raise ServiceError("The note is plain text.")
        # Human intervention explanations have no application length limit.
        res: dict[str, Any] = {"action": action, "note": note if action == "append_intervention_note" else pii.redact(note.strip())}
        if action == "append_intervention_note":
            res.update({k: resolution.get(k) for k in ("note_transfer_id", "note_chunk_index", "note_final_chunk")})
        patch = (resolution or {}).get("facts_patch")
        if patch not in (None, {}):
            try:
                res["facts_patch"] = validate.clean_facts_patch(patch)
            except ValueError as e:
                raise FactsPatchError(str(e)) from e
        if action == "correct_document":
            from .reconciliation import clean_document_patch
            try:
                current = get_state(return_id)
                if res.get("facts_patch") or (current.get("human_edit") or {}).get("status") == "editing":
                    raise ValueError("Complete the current facts edit before submitting a separate document correction.")
                res["document_patch"] = clean_document_patch(resolution.get("document_patch"), current)
            except ValueError as exc:
                raise FactsPatchError(str(exc)) from exc
        res["resolved_by"] = ((resolution or {}).get("resolved_by") or actor.subject) if actor.kind == "system" else actor.subject
        res["resolved_by_role"] = actor.role
        res = {k: v for k, v in res.items() if v not in (None, "")}
        return _invoke(return_id, Command(resume=res), actor, "resolve")


def get_state(return_id: str) -> dict[str, Any]:
    """The ReturnState dict as the graph holds it (messages are LangChain message objects)."""
    return dict(_snapshot(return_id).values)


def original_document(return_id: str, document_id: str, *, actor: Principal) -> tuple[bytes, str]:
    """An authenticated preparer can inspect the immutable source before correcting it."""
    _require(actor, return_id, "resolve_escalation")
    document = next((d for d in get_state(return_id).get("documents_in") or [] if d["id"] == document_id), None)
    if document is None:
        raise ServiceError("No such source document on this return.")
    document = _trusted_document(document)
    if document.get("payload"):
        content, media = json.dumps(pii.redact_deep(document["payload"]), indent=2).encode(), "application/json"
    else:
        path = Path(document["path"])
        media = document.get("media_type") or {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".pdf": "application/pdf", ".json": "application/json"}.get(path.suffix.lower())
        if media not in {"image/jpeg", "image/png", "application/pdf", "application/json"}:
            raise ServiceError("This source format cannot be downloaded.")
        content = uploads.read_bytes(path)
    audit.record("source_document_viewed", actor=actor.label, return_id=return_id, document=document_id)
    return content, media


def state_json(return_id: str) -> dict[str, Any]:
    """A JSON-safe copy of the state with the transcript in place of message objects and SSNs masked."""
    state = get_state(return_id)
    out = {k: v for k, v in state.items() if k != "messages"}
    out = json.loads(json.dumps(out, default=str))
    out["messages"] = transcript_from_state(state)
    out["pending"] = pending(return_id)
    return mask_ssn_deep(out)


def list_returns() -> list[dict[str, Any]]:
    with _index() as conn:
        rows = conn.execute("SELECT * FROM returns ORDER BY updated_at DESC, created_at DESC").fetchall()
    return [_row(r) for r in rows]


def index_row(return_id: str) -> dict[str, Any] | None:
    with _index() as conn:
        row = conn.execute("SELECT * FROM returns WHERE return_id = ?", (return_id,)).fetchone()
    return _row(row) if row else None


def delete_return(return_id: str, *, actor: Principal, reason: str = "deleted by staff") -> None:
    """Remove every trace of a return: checkpoints, uploads, index row, access code, sessions, model spend."""
    _require(actor, return_id, "delete_return")
    with _lock:
        cp = _graph().checkpointer
        if cp is not None:
            cp.delete_thread(return_id)
        with _index() as conn:
            conn.execute("DELETE FROM returns WHERE return_id = ?", (return_id,))
        folder = settings().uploads_dir / Path(return_id).name
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        auth.delete_access(return_id)
        ratelimit.forget(return_id)
    audit.record("return_deleted", actor=actor.label, return_id=return_id, reason=reason)


# ---------------------------------------------------------------- uploads and fixtures
def infer_kind(filename: str) -> str:
    flat = re.sub(r"[^a-z0-9]", "", Path(filename).stem.lower())
    for needle, kind in KIND_HINTS:
        if needle in flat:
            return kind
    return "unknown"


def save_upload(return_id: str, filename: str, data: bytes, *, actor: Principal | None = None) -> dict[str, Any]:
    """Check, clean and store one upload (security/uploads.py) and describe it as a DocumentInput.

    Only JPEG, PNG and PDF are accepted, identified by their bytes, not their name.
    The file is stored encrypted under a random name outside anything the web server serves.
    """
    try:
        doc = uploads.store(return_id, filename, data)
    except uploads.UploadRejected as e:
        audit.record("upload_rejected", actor=actor.label if actor else None, return_id=return_id, reason=str(e), size=len(data or b""))
        raise ServiceError(str(e)) from e
    doc["kind"] = infer_kind(filename)
    audit.record("upload_accepted", actor=actor.label if actor else None, return_id=return_id, document=doc["id"], media_type=doc["media_type"], size=doc["size"], sha256=doc["sha256"])
    return doc


def list_fixtures() -> list[dict[str, Any]]:
    out = []
    for p in sorted(FIXTURES_DIR.glob("case*")):
        if p.is_dir():
            out.append({"name": p.name, "has_intake": (p / "intake.json").exists(), "documents": [d["id"] for d in fixture_documents(p.name)]})
    return out


def fixture_intake(name: str) -> dict[str, Any] | None:
    p = _fixture_dir(name) / "intake.json"
    return json.loads(p.read_text()) if p.exists() else None


def fixture_answers(name: str) -> list[str]:
    p = _fixture_dir(name) / "answers.json"
    return json.loads(p.read_text()) if p.exists() else []


def fixture_documents(name: str) -> list[dict[str, Any]]:
    """The fixture's document payloads as DocumentInputs. A fixture that declares a W-2
    but ships none gets the demo W-2 image (case1), so the image path is exercised."""
    folder = _fixture_dir(name)
    docs: list[dict[str, Any]] = []
    for p in sorted(folder.glob("*.json")):
        if p.stem in ("intake", "answers"):
            continue
        try:
            kind = str(json.loads(p.read_text()).get("kind") or infer_kind(p.name))
        except json.JSONDecodeError:
            kind = infer_kind(p.name)
        docs.append({"id": p.stem, "kind": kind, "path": str(p), "declared_by": "fixture", "uploaded_at": now_iso()})
    intake = fixture_intake(name) or {}
    declares_w2 = "W-2" in (intake.get("documents_declared") or []) or name.startswith("case1")
    if declares_w2 and not any(d["kind"] == "W-2" for d in docs) and DEMO_W2_IMAGE.exists():
        docs.append({"id": DEMO_W2_IMAGE.stem, "kind": "W-2", "path": str(DEMO_W2_IMAGE), "declared_by": "taxpayer", "uploaded_at": now_iso()})
    return docs


def _fixture_dir(name: str) -> Path:
    safe = Path(name).name
    folder = FIXTURES_DIR / safe
    if not safe.startswith("case") or not folder.is_dir():
        raise ServiceError(f"Unknown fixture {name!r}.")
    return folder


# ---------------------------------------------------------------- transcript
def transcript(return_id: str) -> list[dict[str, str]]:
    return transcript_from_state(get_state(return_id))


def transcript_from_state(state: dict[str, Any]) -> list[dict[str, str]]:
    """state["messages"] as [{"role": "assistant"|"taxpayer", "text": ...}], SSNs masked."""
    out: list[dict[str, str]] = []
    for m in state.get("messages") or []:
        if isinstance(m, dict):
            role, content = m.get("role") or m.get("type"), m.get("text") or m.get("content")
        else:
            role, content = getattr(m, "type", None), getattr(m, "content", None)
        if isinstance(content, list):  # content blocks
            content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        text = str(content or "").strip()
        if not text:
            continue
        out.append({"role": "taxpayer" if role in ("human", "user", "taxpayer") else "assistant", "text": mask_ssn(text)})
    return out
