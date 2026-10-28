"""Real-SSN rejection: only 900-series test numbers are ever accepted, at every
door an SSN can come through (the conversation, a dependent, a staff
correction, a loaded checkpoint), and a refused number is never stored."""
from __future__ import annotations

import json

import pytest
from langgraph.types import Command

from conftest import interrupts, new_graph
from silverfile import llm, service
from silverfile.agents import intake as intake_agent
from silverfile.graph import new_return_state
from silverfile.security import audit, auth, pii, validate

REAL = ["123-45-6789", "078-05-1120", "899-99-9999", "001 01 0001", "555443333"]
TEST = ["900-00-0001", "912-34-5678", "999 99 9999", "950123456"]


@pytest.mark.parametrize("ssn", REAL)
def test_a_non_test_number_is_refused(ssn):
    ok, norm = pii.validate_test_ssn(f"my number is {ssn}")
    assert ok is False and norm is not None
    with pytest.raises(pii.RealSSNError):
        pii.require_test_ssn(ssn)
    assert pii.has_real_ssn(ssn)


@pytest.mark.parametrize("ssn", TEST)
def test_a_900_series_number_is_accepted(ssn):
    ok, norm = pii.validate_test_ssn(ssn)
    assert ok is True and norm.startswith("9") and len(norm) == 11
    assert pii.require_test_ssn(ssn) == norm


@pytest.mark.parametrize("text", ["607-555-1234", "EIN 16-1234567", "$18,400.00", "zip 14850-1234", "1234567890"])
def test_other_numbers_are_not_mistaken_for_ssns(text):
    assert pii.find_ssns(text) == []


def _at_slot(slot: str):
    """A conversation driven to `slot` with scripted answers (offline parser)."""
    g = new_graph()
    cfg = {"configurable": {"thread_id": f"ssn-{slot}"}}
    g.invoke(new_return_state(cfg["configurable"]["thread_id"], []), cfg)
    script = {"answering_party": "yes, it's me", "name": "Eleanor Whitfield"}
    while (p := interrupts(g, cfg)) and p[0]["slot"] != slot:
        g.invoke(Command(resume=script[p[0]["slot"]]), cfg)
    return g, cfg


def test_conversation_refuses_a_real_ssn_and_keeps_no_trace_of_it():
    g, cfg = _at_slot("ssn")
    out = g.invoke(Command(resume="it's 123-45-6789"), cfg)
    assert interrupts(g, cfg)[0]["slot"] == "ssn"  # asked again
    assert not out["intake"]["taxpayer"].get("ssn")
    blob = json.dumps(out, default=str)
    assert "123-45-6789" not in blob and "6789" not in json.dumps(out["intake"])
    assert any("non-test SSN" in e and "not stored" in e for e in out["intake"]["volunteered_ssn_events"])
    assert "may be a real Social Security number" in out["messages"][-1].content
    out = g.invoke(Command(resume="912-34-5678"), cfg)
    assert out["intake"]["taxpayer"]["ssn"] == "912-34-5678"


def test_the_ssn_answer_is_never_sent_to_a_model(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete", lambda system, messages, **kw: sent.append(json.dumps(messages)) or llm.LLMResult('{"intent":"answer","updates":{}}', 1, 1, "fake"))
    parsed, tin, _ = intake_agent.llm_parse("ssn", "912-34-5678", {})
    assert sent == [] and tin == 0 and parsed["updates"]["_ssn"] == "912-34-5678"
    # and the facts sent as context for other slots carry no SSN (dependents and spouse are left out)
    intake_agent.llm_parse("dob", "March 3 1950", {"dependents": [{"name": "Ann", "ssn": "923-45-6789"}], "spouse": {"ssn": "934-56-7890"}, "taxpayer": {"ssn": "912-34-5678", "first_name": "Ed"}})
    assert sent and not pii.find_ssns(sent[-1])


def test_a_dependents_real_ssn_is_refused():
    facts = {"dependents": [{"name": "Ann", "relationship": "granddaughter", "ssn": ""}]}
    reply: list[str] = []
    ok, ssn = intake_agent.valid_test_ssn("078-05-1120")
    out = intake_agent.apply("dependent_ssn", {"updates": {"_ssn_ok": ok, "_ssn": ssn}}, facts, reply)
    assert out["dependents"][-1]["ssn"] == "" and "may be a real" in reply[0]


@pytest.mark.parametrize("patch", [{"taxpayer": {"ssn": "123-45-6789"}}, {"spouse": {"ssn": "555-44-3333"}}, {"refund_account_description": "acct 912-34-5678"}])
def test_a_staff_correction_cannot_bring_in_an_ssn(patch):
    with pytest.raises(ValueError):
        validate.clean_facts_patch(patch)


def test_a_staff_correction_with_a_test_ssn_is_accepted():
    assert validate.clean_facts_patch({"spouse": {"ssn": "934 56 7890"}}) == {"spouse": {"ssn": "934-56-7890"}}


def test_a_checkpoint_holding_a_real_ssn_is_refused(data_dir):
    rid = service.create_return([], {"taxpayer": {"first_name": "A", "last_name": "B", "ssn": "912-34-5678"}}, actor=auth.system("test"))
    service._graph().update_state(service._cfg(rid), {"intake": {"taxpayer": {"first_name": "A", "last_name": "B", "ssn": "123-45-6789"}}})
    with pytest.raises(service.StateRejected):
        service.get_state(rid)
    assert audit.recent(1, kind="state_rejected")[0]["return_id"] == rid


def test_a_refused_ssn_is_a_security_event_without_the_number(data_dir):
    rid = service.create_return([], None, actor=auth.system("test"))
    service.run(rid)
    tp = auth.taxpayer_principal(rid)
    for text in ("yes it's me", "Eleanor Whitfield"):
        service.answer(rid, text, actor=tp)
    service.answer(rid, "123-45-6789", actor=tp)
    ev = audit.recent(5, kind="ssn_rejected")
    assert ev and ev[0]["return_id"] == rid and "123-45-6789" not in json.dumps(ev)
