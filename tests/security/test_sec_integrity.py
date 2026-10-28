"""Tamper evidence and state integrity.

- The provenance seal: a completed draft carries a signed hash chain over every
  line and its record; changing any number afterwards is detected, and a seal
  recomputed without the server key does not verify.
- The security log's chain detects an edited or deleted event.
- A checkpoint that was edited, written unencrypted, or no longer matches the
  agents' schema is refused, not loaded (replay and state tampering).
- Encryption keys: production refuses to start without one.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from conftest import CASE1, case1_document_list, load_intake, run_prefilled
from silverfile import service
from silverfile.security import audit, auth, crypto, integrity, validate

SYSTEM = auth.system("test")


@pytest.fixture(scope="module")
def complete():
    _, _, out = run_prefilled("seal", case1_document_list(), load_intake(CASE1))
    assert out["status"] == "complete"
    return out


def test_a_completed_draft_is_sealed_and_verifies(complete):
    seal = complete["provenance_seal"]
    assert seal["lines"] == len(complete["lines"]) and seal["algorithm"].startswith("sha256-chain")
    v = integrity.verify(complete)
    assert v["sealed"] and v["ok"] and v["signature_ok"] and v["altered"] == []


def test_changing_a_number_after_review_is_detected(complete):
    for line in ("1z", "16", "34"):
        tampered = {**complete, "lines": {**complete["lines"], line: complete["lines"][line] + 1}}
        v = integrity.verify(tampered)
        assert not v["ok"] and v["altered"] == [line]
    # editing the record as well as the line (to keep them consistent) is still detected
    p = {**complete["provenance"], "6b": {**complete["provenance"]["6b"], "amount": 1.0}}
    tampered = {**complete, "lines": {**complete["lines"], "6b": 1.0}, "provenance": p}
    assert integrity.verify(tampered)["altered"] == ["6b"]


def test_a_seal_forged_without_the_key_does_not_verify(complete):
    tampered = {**complete, "lines": {**complete["lines"], "34": 99999.0}}
    entries = integrity.chain(tampered["lines"], tampered["provenance"])
    forged = {**complete["provenance_seal"], "entries": entries, "root": entries[-1]["hash"], "signature": hashlib.sha256(entries[-1]["hash"].encode()).hexdigest()}
    v = integrity.verify({**tampered, "provenance_seal": forged})
    assert v["altered"] == [] and v["signature_ok"] is False and v["ok"] is False


def test_the_form_shows_the_seal(complete):
    from silverfile.forms.render import render_html

    assert "Provenance seal verified" in render_html(complete)
    assert "does not match its provenance seal" in render_html({**complete, "lines": {**complete["lines"], "15": 0.0}})


def test_an_edited_or_deleted_security_event_breaks_the_chain(data_dir):
    for i in range(5):
        audit.record("test_event", actor="t", n=i)
    assert audit.verify_chain() == (True, None, 5)
    with sqlite3.connect(data_dir / "security.sqlite") as db:
        db.execute("UPDATE events SET detail = '{\"n\": 99}' WHERE seq = 3")
    ok, bad, _ = audit.verify_chain()
    assert not ok and bad == 3
    with sqlite3.connect(data_dir / "security.sqlite") as db:
        db.execute("DELETE FROM events WHERE seq = 3")
    ok, bad, _ = audit.verify_chain()
    assert not ok and bad == 4


def _checkpoint_rows(data_dir):
    return sqlite3.connect(data_dir / "checkpoints.sqlite")


def test_an_edited_checkpoint_is_refused(data_dir):
    rid = service.create_return(service.fixture_documents("case1_clean"), load_intake(CASE1), actor=SYSTEM)
    service.run(rid)
    assert service.get_state(rid)["status"] == "complete"
    with _checkpoint_rows(data_dir) as db:
        typ, blob = db.execute("SELECT type, checkpoint FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id DESC LIMIT 1", (rid,)).fetchone()
        assert typ.endswith("+fernet") and b"Whitfield" not in blob
        flipped = bytearray(blob)
        flipped[len(flipped) // 2] ^= 0x01
        db.execute("UPDATE checkpoints SET checkpoint = ? WHERE thread_id = ?", (bytes(flipped), rid))
    with pytest.raises(service.StateRejected):
        service.get_state(rid)
    assert audit.recent(1, kind="state_rejected")[0]["return_id"] == rid


def test_an_unencrypted_checkpoint_is_refused(data_dir):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    rid = service.create_return([], None, actor=SYSTEM)
    with _checkpoint_rows(data_dir) as db:
        typ, data = JsonPlusSerializer().dumps_typed({"v": 1, "channel_values": {"status": "complete"}})
        db.execute("UPDATE checkpoints SET type = ?, checkpoint = ? WHERE thread_id = ?", (typ, data, rid))
    with pytest.raises(service.StateRejected):
        service.get_state(rid)


@pytest.mark.parametrize("patch, problem", [
    ({"return_id": "ret-someone-else"}, "return_id does not match"),
    ({"status": "filed"}, "unknown status"),
    ({"lines": {"99z": 5.0}}, "unknown line"),
    ({"lines": {"34": "a lot"}}, "is not a number"),
    ({"escalations": [{"id": "e", "kind": "advice", "status": "resolved"}]}, "resolved without a resolver"),
    ({"review_round": 10**6}, "out of range"),
])
def test_state_that_does_not_match_the_schema_is_refused(patch, problem):
    state = {"return_id": "ret-x", "status": "intake", "tax_year": 2025, **patch}
    assert any(problem in p for p in validate.check_state(state, "ret-x"))


def test_the_fault_hook_is_off_outside_tests(data_dir, monkeypatch):
    monkeypatch.setenv("SILVERFILE_ALLOW_FAULTS", "0")
    with pytest.raises(service.Forbidden):
        service.create_return([], None, {"corrupt_line": "9"}, actor=SYSTEM)
    assert any("fault injection" in p for p in validate.check_state({"return_id": "r", "status": "new", "fault_injection": {"ping_pong": True}}, "r"))
    _, _, out = run_prefilled("faults-off", case1_document_list(), load_intake(CASE1), fault_injection={"corrupt_line": "9", "delta": 100, "rounds": 99})
    assert out["status"] == "complete"  # the hook did nothing


def test_production_refuses_to_run_without_a_data_key(monkeypatch):
    monkeypatch.setenv("SILVERFILE_ENV", "production")
    monkeypatch.delenv("SILVERFILE_DATA_KEY", raising=False)
    with pytest.raises(crypto.KeyMissing):
        crypto.encrypt(b"x")


def test_keys_rotate(monkeypatch):
    monkeypatch.setenv("SILVERFILE_DATA_KEY", "old-key-0000000000000000")
    token = crypto.encrypt(b"secret")
    monkeypatch.setenv("SILVERFILE_DATA_KEY", "new-key-1111111111111111,old-key-0000000000000000")
    assert crypto.decrypt(token) == b"secret"
    monkeypatch.setenv("SILVERFILE_DATA_KEY", "new-key-1111111111111111")
    with pytest.raises(crypto.Undecryptable):
        crypto.decrypt(token)
