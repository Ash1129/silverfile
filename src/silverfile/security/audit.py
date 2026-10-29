"""The security event log: append-only and tamper-evident.

Every security-relevant event (a login, a refused request, a rejected SSN or
upload, an escalation raised or resolved, a budget stop, a refused checkpoint)
is one row. Each row's `hash` is an HMAC over the previous row's hash and this
row's content, keyed with the server's integrity key (security/crypto.py), so
deleting, reordering or editing any row breaks every hash after it, and the
chain cannot be recomputed without the key. `verify_chain` walks it; the admin
audit page shows the result.

Details are redacted (security/pii.redact) before they are written: the log
never holds an SSN or an API key. It records who did what and when, which is
the HITL audit trail the brief asks for.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from ..config import settings
from ..provenance import now_iso
from . import crypto, pii

log = logging.getLogger("silverfile.security")
_lock = threading.Lock()
GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  kind      TEXT NOT NULL,
  actor     TEXT,
  return_id TEXT,
  detail    TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash      TEXT NOT NULL
)
"""


def _db() -> sqlite3.Connection:
    path = settings().security_db
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    if fresh:
        crypto.restrict_file(path)
    return conn


def _digest(prev: str, at: str, kind: str, actor: str | None, return_id: str | None, detail: str) -> str:
    body = json.dumps([prev, at, kind, actor, return_id, detail], separators=(",", ":"))
    return crypto.sign(body.encode())


def record(kind: str, /, *, actor: str | None = None, return_id: str | None = None, **detail: Any) -> None:
    """Append one event of type `kind`. Never raises into the request that caused it."""
    try:
        clean = json.dumps(pii.redact_deep(detail), sort_keys=True, default=str)
        if len(clean) > 4000 and kind != "hitl_workflow":
            clean = json.dumps({"truncated": clean[:3900]})
        actor = pii.redact(actor) if actor else None
        with _lock, _db() as conn:
            row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            at = now_iso()
            conn.execute("INSERT INTO events (at, kind, actor, return_id, detail, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (at, kind, actor, return_id, clean, prev, _digest(prev, at, kind, actor, return_id, clean)))
        log.info("security event %s actor=%s return=%s %s", kind, actor, return_id, clean)
    except Exception as e:  # the log must not take the dashboard down; say so loudly instead
        log.error("could not write security event %s: %s", kind, e)


def recent(limit: int = 200, kind: str | None = None, return_id: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM events", []
    where = []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if return_id:
        where.append("return_id = ?")
        args.append(return_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    with _db() as conn:
        rows = conn.execute(q, args).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


def verify_chain() -> tuple[bool, int | None, int]:
    """(intact, first bad seq or None, rows checked)."""
    prev = GENESIS
    n = 0
    with _db() as conn:
        for r in conn.execute("SELECT * FROM events ORDER BY seq"):
            n += 1
            if r["prev_hash"] != prev or r["hash"] != _digest(prev, r["at"], r["kind"], r["actor"], r["return_id"], r["detail"]):
                return False, r["seq"], n
            prev = r["hash"]
    return True, None, n
