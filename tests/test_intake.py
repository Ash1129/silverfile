"""The Intake agent: parsers in isolation and the conversational graph run."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.types import Command

from conftest import case1_document_list, interrupts, new_graph
from silverfile.agents.intake import apply, next_slot, parse_date, scripted_parse, valid_test_ssn
from silverfile.graph import new_return_state

REAL_LOOKING_SSN = "123-45-6789"
TEST_SSN = "912-34-5678"


# ---------------------------------------------------------------- parsers


@pytest.mark.parametrize("text, ok, ssn", [
    ("900-12-3456", True, "900-12-3456"),
    ("999-99-9999", True, "999-99-9999"),
    ("912 34 5678", True, "912-34-5678"),
    ("it's 912345678", True, "912-34-5678"),
    ("123-45-6789", False, "123-45-6789"),
    ("899-12-3456", False, "899-12-3456"),
    ("899-99-9999", False, "899-99-9999"),
    ("I'd rather not say", False, None),
    ("", False, None),
])
def test_valid_test_ssn(text, ok, ssn):
    assert valid_test_ssn(text) == (ok, ssn)


@pytest.mark.parametrize("text, expected", [
    ("1954-03-14", "1954-03-14"),
    ("3/14/1954", "1954-03-14"),
    ("03/14/1954", "1954-03-14"),
    ("03-14-1954", "1954-03-14"),
    ("March 14, 1954", "1954-03-14"),
    ("March 14 1954", "1954-03-14"),
    ("Mar 14, 1954", "1954-03-14"),
    ("14 March 1954", "1954-03-14"),
    ("  11/02/1949 ", "1949-11-02"),
    ("I was born on 3/14/1954", "1954-03-14"),
    ("born 1954-3-14", "1954-03-14"),
    ("2/30/1954", None),  # not a real date
    ("sometime in the fifties", None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


def test_scripted_parse_third_party_answering():
    up = scripted_parse("answering_party", "I'm her nephew, I'm answering for my aunt", {})["updates"]
    assert up["answering_party"] == "third_party_no_authority"
    up = scripted_parse("answering_party", "I'm her daughter and I hold power of attorney", {})["updates"]
    assert up["answering_party"] == "third_party_with_authority"
    assert scripted_parse("answering_party", "Yes, it's my return", {})["updates"]["answering_party"] == "self"


def test_scripted_parse_documents_and_filing_status():
    kinds = scripted_parse("documents_declared", "I have a W-2 from the library, my SSA-1099, a 1099-R from my pension, and a 1099-INT from the bank", {})["updates"]["kinds"]
    assert set(kinds) == {"W-2", "SSA-1099", "1099-R", "1099-INT"}
    assert scripted_parse("filing_status", "married filing jointly", {})["updates"]["filing_status"] == "mfj"
    assert scripted_parse("filing_status", "I'm not sure", {})["updates"]["filing_status"] is None


def test_apply_rejects_a_real_looking_ssn_and_records_the_event():
    facts = {"answering_party": "self", "taxpayer": {"first_name": "Eleanor", "last_name": "Whitfield"}}
    reply: list[str] = []
    out = apply("ssn", scripted_parse("ssn", REAL_LOOKING_SSN, facts), facts, reply)
    assert "ssn" not in out["taxpayer"]
    assert REAL_LOOKING_SSN not in json.dumps(out)
    assert any("not kept" in r for r in reply)
    assert len(out["volunteered_ssn_events"]) == 1
    assert next_slot(out) == "ssn"  # asked again


def test_next_slot_walks_the_script_in_order():
    facts: dict = {}
    assert next_slot(facts) == "answering_party"
    facts["answering_party"] = "self"
    assert next_slot(facts) == "name"
    facts["taxpayer"] = {"first_name": "E", "last_name": "W", "ssn": TEST_SSN, "dob": "1954-03-14", "blind": False}
    facts["married"] = False
    assert next_slot(facts) == "filing_status"
    facts["filing_status"] = "single"
    assert next_slot(facts) == "dependents"


# ---------------------------------------------------------------- the conversation through the graph

ADVICE_QUESTION = "Should I convert my IRA to a Roth?"
GO_BACK = "Actually, can we go back to my date of birth?"


def _script(answers: list[str]) -> list[str]:
    """case1 answers with the awkward moments inserted."""
    s = list(answers)
    s.insert(s.index(TEST_SSN), REAL_LOOKING_SSN)  # a real-looking SSN first; rejected, then the test number
    docs_idx = next(i for i, a in enumerate(s) if a.startswith("I have a W-2"))
    s.insert(docs_idx + 1, GO_BACK)  # at the "other income" question, go back to the date of birth
    s.insert(docs_idx + 2, "3/14/1954")
    s.insert(s.index("500"), ADVICE_QUESTION)  # at the estimated payments question, ask for advice
    s[s.index("My checking account at Finger Lakes Savings")] = f"My checking account at Finger Lakes Savings, my social is {REAL_LOOKING_SSN}"
    return s


def _drive(g, cfg, script: list[str], documents, *, on_slot=None):
    """Feed answers to question interrupts, documents to the upload interrupt, and
    'continue' to escalations. Returns the transcript of interrupt payloads seen."""
    seen: list[dict] = []
    answers = list(script)
    for _ in range(120):
        pending = interrupts(g, cfg)
        if not pending:
            return seen
        assert len(pending) == 1, pending  # one interrupt at a time
        payload = pending[0]
        seen.append(payload)
        if payload["type"] == "escalation":
            g.invoke(Command(resume={"action": "continue", "resolved_by": "test-reviewer", "note": "noted for the preparer"}), cfg)
            continue
        if on_slot and on_slot(payload):
            continue
        assert answers, f"ran out of scripted answers at {payload.get('slot')}"
        text = answers.pop(0)
        if payload["type"] == "upload":
            g.invoke(Command(resume={"text": text, "documents": documents}), cfg)
        else:
            g.invoke(Command(resume=text), cfg)
    raise AssertionError("the conversation did not finish")


def test_conversational_run_handles_ssn_advice_go_back_and_resume(case1_answers):
    g = new_graph()
    cfg = {"configurable": {"thread_id": "conversation"}}
    g.invoke(new_return_state("conversation", []), cfg)
    documents = case1_document_list()
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    tricked = {"done": False}

    def simulate_gap(payload) -> bool:
        # once, at the blindness question: pretend the session was abandoned two hours ago
        if payload.get("slot") != "blind" or tricked["done"]:
            return False
        tricked["done"] = True
        intake = dict(g.get_state(cfg).values["intake"])
        intake["_last_activity_at"] = two_hours_ago
        g.update_state(cfg, {"intake": intake}, as_node="orchestrator")
        g.invoke(None, cfg)  # re-asks the same question, now with the summary
        return True

    seen = _drive(g, cfg, _script(case1_answers), documents, on_slot=simulate_gap)
    state = g.get_state(cfg).values
    questions = [p for p in seen if p["type"] in ("question", "upload")]
    slots = [p["slot"] for p in questions]

    # one question per interrupt, in plain language
    for p in questions:
        assert list(p).count("text") == 1 and isinstance(p["text"], str) and p["text"]
        assert p["text"].count("?") <= 2, p["text"]
        assert p["summary"] is not None

    # the real-looking SSN was rejected, re-asked, and never stored in the facts
    assert slots[2:4] == ["ssn", "ssn"]
    assert state["intake"]["taxpayer"]["ssn"] == TEST_SSN
    assert REAL_LOOKING_SSN not in json.dumps(state["intake"])
    assert REAL_LOOKING_SSN not in json.dumps(state["escalations"])
    assert REAL_LOOKING_SSN not in json.dumps(state["events"])
    assert "[number removed]" in state["intake"]["refund_account_description"]
    events = state["intake"]["volunteered_ssn_events"]
    assert len(events) == 2
    assert "slot ssn" in events[0] and "rejected and not stored" in events[0]
    assert "slot refund_account" in events[1] and "NOT a test number" in events[1]

    # the advice question was noted, escalated, and the same question asked again afterwards
    assert state["intake"]["noted_questions"] == [ADVICE_QUESTION]
    advice = [e for e in state["escalations"] if e["kind"] == "advice"]
    assert len(advice) == 1 and advice[0]["status"] == "resolved" and advice[0]["raised_by"] == "intake"
    esc_idx = next(i for i, p in enumerate(seen) if p["type"] == "escalation")
    assert seen[esc_idx - 1]["slot"] == "estimated_payments" and seen[esc_idx + 1]["slot"] == "estimated_payments"
    assert state["intake"]["estimated_payments"] == 500.0

    # going back re-asked only the date of birth; the earlier answers survived
    gb = slots.index("other_income")
    assert slots[gb:gb + 3] == ["other_income", "dob", "other_income"]
    assert state["intake"]["taxpayer"]["dob"] == "1954-03-14"
    assert state["intake"]["taxpayer"]["first_name"] == "Eleanor" and state["intake"]["taxpayer"]["last_name"] == "Whitfield"
    assert state["intake"]["filing_status"] == "single"
    assert state["intake"]["married"] is False
    assert set(state["intake"]["documents_declared"]) == {"W-2", "SSA-1099", "1099-R", "1099-INT"}
    assert state["intake"]["other_income_sources"] == []
    assert any("went back to dob" in e for e in state["events"])

    # the resume summary after a gap
    welcome = [p for p in questions if p["text"].startswith("Welcome back")]
    assert len(welcome) == 1 and welcome[0]["slot"] == "blind"
    assert "Eleanor Whitfield" in welcome[0]["text"] and "1954-03-14" in welcome[0]["text"]
    assert slots.count("blind") == 2
    assert any(m.content.startswith("Welcome back") for m in state["messages"])

    # the upload interrupt carried the documents and the run completed with lines
    assert [p["type"] for p in questions if p["slot"] == "upload"] == ["upload"]
    assert state["intake_complete"] is True
    assert state["intake"]["_confirmed"] is True
    assert state["status"] == "complete"
    assert set(state["extracted"]) == {"w2-1", "ssa-1", "int-1", "r-1"}
    assert state["lines"]["1z"] == 18_400.0
    assert state["lines"]["6b"] == 13_205.84
    assert state["lines"]["16"] == 2_509.26
    assert state["lines"]["34"] == 100.74
    assert interrupts(g, cfg) == []


def test_plain_case1_conversation_matches_the_prefilled_numbers(case1_answers):
    g = new_graph()
    cfg = {"configurable": {"thread_id": "plain"}}
    g.invoke(new_return_state("plain", []), cfg)
    seen = _drive(g, cfg, case1_answers, case1_document_list())
    state = g.get_state(cfg).values
    assert [p["type"] for p in seen].count("escalation") == 0
    assert len(seen) == len(case1_answers)  # one interrupt per answer
    assert state["status"] == "complete"
    assert state["lines"]["15"] == 22_898.01 and state["lines"]["16"] == 2_509.26
    assert state["intake"].get("volunteered_ssn_events", []) == []
    assert state["intake"].get("noted_questions", []) == []
    assert len(state["messages"]) >= 2 * len(case1_answers)


def test_third_party_answer_pauses_the_intake():
    g = new_graph()
    cfg = {"configurable": {"thread_id": "third-party-intake"}}
    g.invoke(new_return_state("third-party-intake", []), cfg)
    g.invoke(Command(resume="I'm her nephew, I'm answering for my aunt"), cfg)
    pending = interrupts(g, cfg)
    assert len(pending) == 1 and pending[0]["type"] == "escalation" and pending[0]["escalation"]["kind"] == "exploitation"
    state = g.get_state(cfg).values
    assert state["intake"]["answering_party"] == "third_party_no_authority"
    assert state["status"] == "waiting_human"
    final = g.invoke(Command(resume={"action": "halt", "resolved_by": "test-reviewer", "note": "no authority shown"}), cfg)
    assert final["status"] == "halted"
