"""The provenance seal: proof that no number changed after review.

When a draft completes (finalize, after Review passed or a person accepted it),
`seal` walks every line in form order and builds a hash chain:

    h_0 = SHA-256("silverfile-seal-v1")
    h_i = SHA-256(h_{i-1} || canonical JSON of {line, amount, provenance record})

The last hash is the root; the root is signed with the server's HMAC key
(security/crypto.py). `verify` rebuilds the chain from the current state and
names the first line whose entry no longer matches. Anyone with the database
could recompute plain hashes, which is why the root is signed: without the key
a changed number cannot be given a matching seal. The checkpoint itself is
also encrypted with an authenticated cipher, so this is a second, independent
check that a preparer can see on the draft.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..provenance import ALL_LINES, now_iso
from . import crypto

ALGORITHM = "sha256-chain/hmac-sha256-root"
GENESIS = hashlib.sha256(b"silverfile-seal-v1").hexdigest()


def _ordered(lines: dict[str, Any]) -> list[str]:
    return [l for l in ALL_LINES if l in lines] + sorted(l for l in lines if l not in ALL_LINES)


def _entry(line: str, amount: Any, record: Any) -> bytes:
    return json.dumps({"line": line, "amount": amount, "provenance": record}, sort_keys=True, separators=(",", ":"), default=str).encode()


def chain(lines: dict[str, Any], provenance: dict[str, Any]) -> list[dict[str, str]]:
    prev, out = GENESIS, []
    for line in _ordered(lines):
        prev = hashlib.sha256(prev.encode() + _entry(line, lines[line], provenance.get(line))).hexdigest()
        out.append({"line": line, "hash": prev})
    return out


def seal(state: dict[str, Any]) -> dict[str, Any]:
    entries = chain(state.get("lines") or {}, state.get("provenance") or {})
    root = entries[-1]["hash"] if entries else GENESIS
    return {
        "algorithm": ALGORITHM, "root": root, "signature": crypto.sign(root.encode()), "entries": entries,
        "sealed_at": now_iso(), "review_round": int(state.get("review_round", 0)), "computation_version": int(state.get("computation_version", 0)),
        "lines": len(entries),
    }


def verify(state: dict[str, Any]) -> dict[str, Any]:
    """{"sealed", "ok", "signature_ok", "altered": [lines], "added": [...], "removed": [...]} for display and tests."""
    s = state.get("provenance_seal")
    if not s:
        return {"sealed": False, "ok": False, "signature_ok": False, "altered": [], "added": [], "removed": []}
    now = {e["line"]: e["hash"] for e in chain(state.get("lines") or {}, state.get("provenance") or {})}
    then = {e["line"]: e["hash"] for e in s.get("entries") or []}
    added = [l for l in now if l not in then]
    removed = [l for l in then if l not in now]
    # the chain makes every entry after a change differ too; name the first differing line
    altered = []
    for e in s.get("entries") or []:
        if e["line"] in now and now[e["line"]] != e["hash"]:
            altered.append(e["line"])
            break
    signature_ok = crypto.verify_signature(str(s.get("root", "")).encode(), s.get("signature", ""))
    root_ok = bool(s.get("entries")) and s["entries"][-1]["hash"] == s.get("root") or (not s.get("entries") and s.get("root") == GENESIS)
    ok = signature_ok and root_ok and not altered and not added and not removed
    return {"sealed": True, "ok": ok, "signature_ok": signature_ok, "altered": altered, "added": added, "removed": removed, "sealed_at": s.get("sealed_at"), "review_round": s.get("review_round"), "root": s.get("root")}
