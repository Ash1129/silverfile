"""Volunteered-SSN redaction: an SSN the system did not ask for is handled
deliberately. It is not stored anywhere, it is removed from the transcript
before the checkpoint is written, the taxpayer is told, and the event is
logged without the number. Logs are scrubbed whatever library writes them."""
from __future__ import annotations

import json
import logging

from langgraph.types import Command

from conftest import interrupts, new_graph
from silverfile import service
from silverfile.graph import new_return_state
from silverfile.security import audit, auth, pii


def _converse(thread: str, answers: list[str]):
    g = new_graph()
    cfg = {"configurable": {"thread_id": thread}}
    out = g.invoke(new_return_state(thread, []), cfg)
    for a in answers:
        out = g.invoke(Command(resume=a), cfg)
    return g, cfg, out


def test_an_ssn_volunteered_at_another_question_is_removed_and_not_stored():
    # the taxpayer gives their name and, unasked, a real-looking SSN; then a test SSN where one is not asked for either
    g, cfg, out = _converse("volunteer", ["yes it's me", "Eleanor Whitfield 123-45-6789"])
    tp = out["intake"]["taxpayer"]
    assert tp["last_name"] == "Whitfield" and not tp.get("ssn")
    said = [m.content for m in out["messages"]]
    assert not any(pii.find_ssns(t) for t in said), "the number survived in the transcript"
    assert any("[number removed]" in t for t in said)
    assert any("I didn't need it here, so I haven't kept it" in t for t in said)
    ev = out["intake"]["volunteered_ssn_events"][-1]
    assert "NOT a test number" in ev and "not stored" in ev and not pii.find_ssns(ev)
    assert "6789" not in json.dumps(out, default=str)


def test_a_volunteered_test_ssn_is_also_not_kept_outside_its_question():
    _, _, out = _converse("volunteer-test", ["yes, me", "Harold Benson 912-34-5678"])
    assert not out["intake"]["taxpayer"].get("ssn")
    assert "test series" in out["intake"]["volunteered_ssn_events"][-1]


def test_masking_and_redaction():
    assert pii.mask("SSN 912-34-5678 on file") == "SSN ***-**-5678 on file"
    assert pii.redact("SSN 912 34 5678, key sk-ant-api03-abcdefghijklmnop") == "SSN [SSN], key sk-ant-[REDACTED]"
    assert pii.redact_deep({"a": ["912345678", {"b": "x 123-45-6789"}]}) == {"a": ["[SSN]", {"b": "x [SSN]"}]}


def test_every_log_record_is_redacted(caplog):
    pii.install_log_redaction()
    with caplog.at_level(logging.INFO):
        logging.getLogger("some.library").info("user said %s with key %s", "123-45-6789", "sk-ant-api03-zzzzzzzzzzzzzzzz")
    assert "123-45-6789" not in caplog.text and "zzzzzzzz" not in caplog.text
    assert "[SSN]" in caplog.text and "sk-ant-[REDACTED]" in caplog.text


def test_security_log_details_are_redacted(data_dir):
    audit.record("test_event", actor="tester 912-34-5678", note="the SSN 123-45-6789 was typed")
    row = audit.recent(1)[0]
    assert "[SSN]" in row["detail"]["note"] and "[SSN]" in row["actor"]
    assert not pii.find_ssns(json.dumps(row))


def test_the_dashboard_view_of_a_return_masks_ssns(data_dir):
    rid = service.create_return([], {"taxpayer": {"first_name": "Eleanor", "last_name": "Whitfield", "ssn": "912-34-5678"}}, actor=auth.system("test"))
    blob = json.dumps(service.state_json(rid))
    assert "912-34-5678" not in blob and "***-**-5678" in blob


def test_redaction_keeps_log_arguments_intact_for_formatters_that_unpack_them(caplog):
    """uvicorn's access log unpacks exactly five arguments; redaction must not flatten them."""
    from uvicorn.logging import AccessFormatter

    pii.install_log_redaction()
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/t?ssn=123-45-6789", "1.1", 200)
    record = caplog.records[-1]
    assert len(record.args) == 5 and record.args[4] == 200
    line = AccessFormatter("%(client_addr)s %(request_line)s %(status_code)s").format(record)
    assert "GET /t?ssn=[SSN]" in line and "123-45-6789" not in line
