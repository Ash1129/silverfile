"""Thin service layer shared by the dashboard and the CLI.

It drives the compiled graph (see graph.py) one return at a time and keeps a
small SQLite index at settings().index_db so the list page never has to load a
checkpoint. The index holds the taxpayer's name, status and a plain-language
summary only; it never holds an SSN.

Contract with the graph
- create_return seeds a thread with new_return_state (update_state on a fresh
  thread is treated as START input); run() invokes until the first interrupt.
- pending() is the first interrupt payload: {"type": "question"|"upload"|
  "escalation", ...} or None when nothing is waiting.
- answer() resumes a question or upload interrupt; resolve() resumes an
  escalation interrupt. The index is refreshed after every invoke.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from langgraph.types import Command

from .config import ROOT, settings
from .forms.render import mask_ssn, mask_ssn_deep
from .graph import app, new_return_state
from .memory import session_summary
from .provenance import now_iso

FIXTURES_DIR = ROOT / "data" / "fixtures"
DEMO_W2_IMAGE = ROOT / "demo" / "w2_image_1.jpg"
UPLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".json"}
KIND_HINTS = (("1099int", "1099-INT"), ("1099div", "1099-DIV"), ("1099r", "1099-R"), ("ssa", "SSA-1099"), ("w2", "W-2"))
ACTIONS = ("continue", "halt", "accept")

_lock = threading.RLock()  # graph invokes are serialised per process (one SQLite checkpointer)


class ServiceError(ValueError):
    """A request that does not fit the return's current state (bad input, nothing pending)."""


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
    conn = sqlite3.connect(str(settings().index_db), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def _graph():
    return app()


def _cfg(return_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": return_id}}


def _interrupts(snap) -> list[Any]:
    return [i.value for t in snap.tasks for i in t.interrupts]


def _snapshot(return_id: str):
    snap = _graph().get_state(_cfg(return_id))
    if not snap.values:
        raise KeyError(return_id)
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
            (return_id, values.get("created_at") or now, now, mask_ssn(taxpayer_name(values)), status, waiting, mask_ssn(summary), pending_type, fixture),
        )
    return values


# ---------------------------------------------------------------- lifecycle
def new_return_id() -> str:
    return "ret-" + uuid.uuid4().hex[:8]


def create_return(documents: list[dict[str, Any]] | None = None, intake: dict[str, Any] | None = None, fault_injection: dict[str, Any] | None = None, *, return_id: str | None = None, fixture: str | None = None) -> str:
    """Seed a new thread (nothing runs yet) and index it. Call run() to start it."""
    rid = return_id or new_return_id()
    state = new_return_state(rid, documents=documents, fault_injection=fault_injection, intake=intake)
    with _lock:
        _graph().update_state(_cfg(rid), state)
        _refresh(rid, fixture=fixture)
    return rid


def run(return_id: str) -> dict[str, Any]:
    """Start or advance the return: invoke the graph until it finishes or pauses."""
    with _lock:
        snap = _snapshot(return_id)
        if snap.next:
            _graph().invoke(None, _cfg(return_id))
        return _refresh(return_id)


def pending(return_id: str) -> dict[str, Any] | None:
    """The first interrupt payload waiting on a person, or None."""
    pend = _interrupts(_snapshot(return_id))
    return pend[0] if pend else None


def answer(return_id: str, text: str, documents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Answer the pending question. At the upload step, `documents` are appended to the return."""
    with _lock:
        p = pending(return_id)
        if not p or p.get("type") not in ("question", "upload"):
            raise ServiceError("No question is waiting for the taxpayer on this return.")
        docs = list(documents or [])
        if docs and p.get("type") != "upload":
            raise ServiceError("Documents can only be attached at the upload step of the conversation.")
        resume: Any = {"text": text, "documents": docs} if p.get("type") == "upload" else text
        _graph().invoke(Command(resume=resume), _cfg(return_id))
        return _refresh(return_id)


def resolve(return_id: str, resolution: dict[str, Any]) -> dict[str, Any]:
    """Resolve the open escalation: {"action": continue|halt|accept, "resolved_by", "note", "facts_patch"}."""
    with _lock:
        p = pending(return_id)
        if not p or p.get("type") != "escalation":
            raise ServiceError("No escalation is waiting on this return.")
        action = (resolution or {}).get("action")
        if action not in ACTIONS:
            raise ServiceError(f"action must be one of {', '.join(ACTIONS)}.")
        res = {k: v for k, v in resolution.items() if v not in (None, "")}
        res.setdefault("resolved_by", "kushim-employee")
        patch = res.get("facts_patch")
        if patch is not None and not isinstance(patch, dict):
            raise ServiceError("facts_patch must be a JSON object.")
        _graph().invoke(Command(resume=res), _cfg(return_id))
        return _refresh(return_id)


def get_state(return_id: str) -> dict[str, Any]:
    """The ReturnState dict as the graph holds it (messages are LangChain message objects)."""
    return dict(_snapshot(return_id).values)


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
    return [dict(r) for r in rows]


def index_row(return_id: str) -> dict[str, Any] | None:
    with _index() as conn:
        row = conn.execute("SELECT * FROM returns WHERE return_id = ?", (return_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- uploads and fixtures
def infer_kind(filename: str) -> str:
    flat = re.sub(r"[^a-z0-9]", "", Path(filename).stem.lower())
    for needle, kind in KIND_HINTS:
        if needle in flat:
            return kind
    return "unknown"


def save_upload(return_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """Store an uploaded image or .json payload under uploads/<return_id>/ and describe it as a DocumentInput."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename or "upload").name).strip("._") or "upload"
    suffix = Path(safe).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise ServiceError(f"Unsupported file type {suffix or '(none)'}; upload a .jpg/.png image or a .json payload.")
    folder = settings().uploads_dir / return_id
    folder.mkdir(parents=True, exist_ok=True)
    dest, n = folder / safe, 1
    while dest.exists():
        dest = folder / f"{Path(safe).stem}-{n}{suffix}"
        n += 1
    dest.write_bytes(data)
    kind = infer_kind(safe)
    if suffix == ".json":
        try:
            payload = json.loads(data.decode("utf-8"))
            if isinstance(payload, dict) and payload.get("kind") and kind == "unknown":
                kind = str(payload["kind"])
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ServiceError(f"{safe} is not valid JSON: {e}") from e
    return {"id": dest.stem, "kind": kind, "path": str(dest), "declared_by": "dashboard", "uploaded_at": now_iso()}


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
