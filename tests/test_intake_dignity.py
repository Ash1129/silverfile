"""Guardrails B: accessibility and dignity in the intake dialogue (brief §4.3), with messy users.

Each test is one scripted taxpayer who behaves the way real people do: they mention their
family, repeat themselves, change their minds, wander, say "I don't know", ask for advice,
or let someone else take over the keyboard. The assertions are the dignity rules:

- one question at a time, in plain language, with tax terms explained the first time;
- earlier answers are never lost;
- no SSN is asked for unless the form needs it, and an unrequested one is never kept;
- "I'm not sure" is an acceptable answer that reaches a person;
- a third party acting for the taxpayer without authority stops the return (§4.4 condition 6),
  but merely mentioning a spouse or a relative does not.

The tests drive the intake node one turn at a time, answering its question in place of the
LangGraph interrupt, so a whole conversation runs in milliseconds and offline.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from silverfile.agents import intake
from silverfile.agents.intake import JARGON, next_slot

TEST_SSN = "912-34-5678"
SPOUSE_TEST_SSN = "934-56-7890"
REAL_LOOKING_SSN = "123-45-6789"

# A married couple filing jointly, answered plainly. Each test swaps in its own awkward moments.
MARRIED = {
    "answering_party": "Yes, it's my return",
    "name": "Robert Whitfield",
    "ssn": TEST_SSN,
    "dob": "3/14/1950",
    "blind": "No",
    "marital": "Married",
    "filing_status": "Married filing jointly",
    "spouse_name": "My wife is Mary Whitfield",
    "spouse_ssn": SPOUSE_TEST_SSN,
    "spouse_dob": "June 1, 1952",
    "spouse_blind": "No",
    "dependents": "None",
    "can_be_claimed": "No, my wife and I support ourselves",
    "documents_declared": "My pension 1099-R and both our SSA-1099s",
    "other_income": "No",
    "estimated_payments": "None",
    "schedule1_adjustments": "None",
    "schedule1_other_income": "None",
    "schedule2_line3": "None",
    "schedule3_line8": "None",
    "refund_account": "Our joint checking at Cayuga Trust",
    "refund_account_is_own": "Yes",
    "upload": "done",
    "confirm": "Yes",
}
SINGLE = {**MARRIED, "name": "Eleanor Whitfield", "marital": "Single", "filing_status": "Single",
          "documents_declared": "A W-2, my SSA-1099 and a 1099-INT from the bank"}


@contextmanager
def _answering(answer: str) -> Iterator[dict[str, Any]]:
    """Stand in for the LangGraph interrupt: capture the question, reply with `answer`."""
    asked: dict[str, Any] = {}
    original = intake.interrupt

    def fake_interrupt(payload: dict[str, Any]) -> str:
        asked.update(payload)
        return answer

    intake.interrupt = fake_interrupt
    try:
        yield asked
    finally:
        intake.interrupt = original


def converse(script: dict[str, str] | list[str], *, extra: dict[str, list[str]] | None = None, stop_at: str | None = None,
             max_turns: int = 60) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Hold a conversation. `script` answers by slot (or in order, for a list); `extra` lists
    answers to give *before* the scripted one at a slot (a wrong number, a question, a detour).
    Returns the final state and every question asked, with the answer given."""
    state: dict[str, Any] = {"intake": {}, "escalations": [], "messages": [], "return_id": "dignity-test"}
    queue = list(script) if isinstance(script, list) else None
    detours = {k: list(v) for k, v in (extra or {}).items()}
    turns: list[dict[str, Any]] = []
    for _ in range(max_turns):
        slot = next_slot(state["intake"])
        if slot is None or slot == stop_at:
            break
        if detours.get(slot):
            answer = detours[slot].pop(0)
        elif queue is not None:
            answer = queue.pop(0)
        else:
            answer = script[slot]  # type: ignore[index]
        with _answering(answer) as asked:
            out = intake.run_intake(state)
        turns.append({**asked, "answer": answer, "reply": " ".join(m.content for m in out.get("messages", [])[2:])})
        state = {**state, "intake": out["intake"], "escalations": state["escalations"] + out.get("escalations", []),
                 "messages": state["messages"] + out.get("messages", [])}
    return state, turns


def kinds(state: dict[str, Any]) -> list[str]:
    return [e["kind"] for e in state["escalations"]]


# ---------------------------------------------------------------- one question at a time, plain language
@pytest.mark.parametrize("script", [SINGLE, MARRIED], ids=["single", "married"])
def test_every_question_is_one_question_in_plain_language(script):
    _, turns = converse(script)
    assert turns, "no questions were asked"
    for t in turns:
        text = t["text"]
        # never a compound question: at most one question mark (the upload step is an instruction, not a question)
        assert text.count("?") == (0 if t["slot"] == "upload" else 1), (t["slot"], text)
        for word in JARGON:
            assert f" {word.lower()} " not in f" {text.lower()} ", (t["slot"], word)


def test_tax_terms_are_explained_once_the_first_time_they_appear():
    state, turns = converse(MARRIED)
    by_slot = {t["slot"]: t["text"] for t in turns}
    assert "Your filing status is the category" in by_slot["filing_status"]
    assert "The standard deduction is the fixed amount" in by_slot["blind"]
    assert "A dependent is a child or relative" in by_slot["dependents"]
    assert "Estimated tax payments are amounts" in by_slot["estimated_payments"]
    # explained once: the later "dependent" question does not repeat it
    assert "A dependent is a child or relative" not in by_slot["can_be_claimed"]
    everything = " ".join(t["text"] for t in turns)
    assert everything.count("A dependent is a child or relative") == 1
    assert set(state["intake"]["_glossed"]) >= {"filing status", "standard deduction", "dependent", "estimated tax payment"}


# ---------------------------------------------------------------- a spouse or relative mentioned is not a third party
def test_a_married_couple_mentioning_each_other_is_not_an_exploitation_case():
    # "My wife is Mary Whitfield", "my wife and I support ourselves", "our joint checking", and a remark in passing
    state, _ = converse(MARRIED, extra={"other_income": ["My wife handled the money for years, so bear with me"]})
    facts = state["intake"]
    assert facts["answering_party"] == "self"
    assert "exploitation" not in kinds(state)
    assert facts["spouse"]["first_name"] == "Mary" and facts["spouse"]["last_name"] == "Whitfield"
    assert next_slot(facts) is None  # the conversation reached the end


@pytest.mark.parametrize("answer", [
    "my daughter is typing this for me",
    "I'm answering for my mother, she's resting",
    "This is her caregiver, I'm filling it in for her",
])
def test_someone_taking_over_mid_conversation_stops_the_return(answer):
    state, turns = converse(SINGLE, extra={"dob": [answer]}, stop_at="blind")
    facts = state["intake"]
    assert facts["answering_party"] == "third_party_no_authority"
    assert kinds(state).count("exploitation") == 1
    takeover = next(t for t in turns if t["answer"] == answer)
    assert "confirm that's authorised" in takeover["reply"]


def test_a_helper_who_states_authority_is_noted_for_the_preparer_not_stopped():
    state, turns = converse(SINGLE, extra={"dob": ["I'm typing for her now, I have power of attorney"]}, stop_at="blind")
    facts = state["intake"]
    assert facts["answering_party"] == "third_party_with_authority"
    assert "exploitation" not in kinds(state)
    assert any("power of attorney" in q for q in facts["noted_questions"])


@pytest.mark.parametrize("answer, expected", [
    ("Yes, it's my return", "self"),
    ("Yes, my son is helping me today", "self"),
    ("No, I'm her daughter", "third_party_no_authority"),
    ("I'm her nephew, I'm answering for my aunt", "third_party_no_authority"),
    ("I'm her daughter and I hold power of attorney", "third_party_with_authority"),
])
def test_who_is_answering_at_the_first_question(answer, expected):
    state, _ = converse([answer], max_turns=1)
    assert state["intake"]["answering_party"] == expected


# ---------------------------------------------------------------- repetition, contradiction, drift
def test_a_correction_replaces_only_that_answer():
    # the first date given is a slip; at a later question they ask to go back and give the right one
    state, turns = converse(SINGLE, extra={"dob": ["3/14/1950", "3/15/1950"], "other_income": ["Actually, can we go back to my date of birth?"]})
    slots = [t["slot"] for t in turns]
    i = slots.index("other_income")
    assert slots[i:i + 3] == ["other_income", "dob", "other_income"]
    facts = state["intake"]
    assert facts["taxpayer"]["dob"] == "1950-03-15"  # the correction stuck
    assert facts["taxpayer"]["first_name"] == "Eleanor" and facts["taxpayer"]["ssn"] == TEST_SSN
    assert facts["filing_status"] == "single"
    assert set(facts["documents_declared"]) == {"W-2", "SSA-1099", "1099-INT"}
    assert next_slot(facts) is None


def test_an_answer_that_does_not_fit_is_asked_again_without_losing_anything():
    state, turns = converse(SINGLE, extra={"dob": ["oh, sometime after the war", "I've lived here forty years, you know"]})
    dob_turns = [t for t in turns if t["slot"] == "dob"]
    assert len(dob_turns) == 3  # two wanderings, then the date
    assert "couldn't read that as a date" in dob_turns[0]["reply"]
    facts = state["intake"]
    assert facts["taxpayer"]["dob"] == "1950-03-14"
    assert facts["taxpayer"]["last_name"] == "Whitfield" and facts["taxpayer"]["ssn"] == TEST_SSN


def test_a_contradiction_at_the_summary_reopens_only_that_answer():
    plain, _ = converse(SINGLE)
    state, turns = converse(SINGLE, extra={"confirm": ["No, my date of birth is wrong"]})
    slots = [t["slot"] for t in turns]
    assert slots[-3:] == ["confirm", "dob", "confirm"]  # summary, the one fix, summary again
    strip = lambda f: {k: v for k, v in f.items() if k not in ("_turns", "_last_activity_at")}  # noqa: E731
    assert strip(state["intake"]) == strip(plain["intake"])  # nothing else was lost on the way


# ---------------------------------------------------------------- honest uncertainty
def test_im_not_sure_is_an_answer_that_reaches_a_person():
    state, turns = converse(SINGLE, extra={"filing_status": ["I'm not sure"]}, stop_at="dependents")
    facts = state["intake"]
    assert facts.get("filing_status") is None and facts["_filing_status_unsure"] is True
    assert "Taxpayer is not sure of their filing status." in facts["noted_questions"]
    assert "filing_status" in kinds(state)
    assert "noted it for the preparer" in turns[-1]["reply"]


@pytest.mark.parametrize("question", [
    "Would it help to file separately?",
    "Which is better, single or head of household?",
    "Can I deduct my medications?",
    "Should I convert my IRA to a Roth?",
])
def test_an_advice_question_is_noted_and_the_same_question_asked_again(question):
    state, turns = converse(MARRIED, extra={"filing_status": [question]}, stop_at="spouse_name")
    assert kinds(state).count("advice") == 1
    assert state["intake"]["noted_questions"] == [question]
    slots = [t["slot"] for t in turns]
    assert slots.count("filing_status") == 2  # asked again after the advice question
    assert "not able to give advice" in turns[slots.index("filing_status")]["reply"]
    assert state["intake"]["filing_status"] == "mfj"


def test_naming_a_filing_status_is_not_mistaken_for_advice():
    state, _ = converse({**MARRIED, "filing_status": "Married filing separately"}, stop_at="spouse_name")
    assert "advice" not in kinds(state) and state["intake"]["filing_status"] == "mfs"


# ---------------------------------------------------------------- no unnecessary PII
def test_a_single_filer_is_asked_for_exactly_one_ssn():
    _, turns = converse(SINGLE)
    assert [t["slot"] for t in turns if "Social Security number" in t["text"]] == ["ssn"]


def test_the_spouse_ssn_is_asked_only_for_a_joint_return_and_must_be_a_test_number():
    state, turns = converse(MARRIED, extra={"spouse_ssn": [REAL_LOOKING_SSN]})
    slots = [t["slot"] for t in turns]
    assert slots.count("spouse_ssn") == 2  # the real-looking one was refused, then asked again
    assert "have not kept it" in turns[slots.index("spouse_ssn")]["reply"]
    facts = state["intake"]
    assert facts["spouse"]["ssn"] == SPOUSE_TEST_SSN
    assert REAL_LOOKING_SSN not in json.dumps(facts) and REAL_LOOKING_SSN not in json.dumps([m.content for m in state["messages"]])


def test_an_unrequested_ssn_is_not_kept_and_the_person_is_told():
    state, turns = converse(SINGLE, extra={"can_be_claimed": [f"No. Here's my social, {REAL_LOOKING_SSN}"]})
    facts = state["intake"]
    assert REAL_LOOKING_SSN not in json.dumps(facts)
    assert REAL_LOOKING_SSN not in json.dumps([m.content for m in state["messages"]])
    assert any("NOT a test number" in e and "not stored" in e for e in facts["volunteered_ssn_events"])
    detour = next(t for t in turns if t["answer"].startswith("No. Here's my social"))
    assert "I didn't need it here, so I haven't kept it" in detour["reply"]
    assert facts["can_be_claimed_as_dependent"] is False  # the answer itself ("No") still counted


@pytest.mark.parametrize("answer", ["None", "no more", "That's everyone", "I don't have any"])
def test_no_dependents_said_any_natural_way_is_not_taken_as_a_name(answer):
    state, _ = converse({**SINGLE, "dependents": answer}, stop_at="documents_declared")
    assert state["intake"]["dependents"] == []

# ---------------------------------------------------------------- unknown facts are not negative answers
@pytest.mark.parametrize("answer", ["I'm not sure", "I don't know", "No idea", "I cannot remember", "Not sure, maybe 500"])
def test_uncertain_answers_are_not_false_or_zero(answer):
    from silverfile.agents.intake import parse_yes_no, parse_money, scripted_parse, apply
    assert parse_yes_no(answer) is None
    assert parse_money(answer) is None
    for slot, field in (("can_be_claimed", "can_be_claimed_as_dependent"), ("estimated_payments", "estimated_payments")):
        parsed = scripted_parse(slot, answer, {})
        assert parsed["intent"] == "unsure"
        facts = apply(slot, parsed, {}, [])
        assert facts[field] is None and field in facts["_unknown_fields"]


@pytest.mark.parametrize("answer", ["No", "None", "Nothing", "Zero"])
def test_explicit_negative_amount_is_still_zero(answer):
    from silverfile.agents.intake import parse_money
    assert parse_money(answer) == 0.0


def test_unknown_payment_pauses_and_a_confirmed_zero_can_resume(data_dir):
    from silverfile import service
    from silverfile.security import auth
    system = auth.system("unknown-test")
    facts = {**service.fixture_intake("case1_clean"), "estimated_payments": None, "_unknown_fields": ["estimated_payments"]}
    rid = service.create_return(service.fixture_documents("case1_clean"), facts, actor=system)
    out = service.run(rid)
    assert out["status"] == "waiting_human" and not out["lines"]
    out = service.resolve(rid, {"action": "continue"}, actor=system)
    assert out["hitl_log"][-1]["result"] == "no"
    out = service.resolve(rid, {"action": "continue", "facts_patch": {"estimated_payments": 0}}, actor=system)
    assert out["status"] == "complete" and out["lines"]["26"] == 0
    assert out["intake"]["_unknown_fields"] == []


def test_unknown_box2b_is_preserved_and_blocks_computation(case1_intake):
    from conftest import document, run_prefilled
    from silverfile.security.validate import checkbox
    assert checkbox(False) is False and checkbox("no") is False
    assert checkbox(None) is None and checkbox("not sure") is None
    doc = document("uncertain-r", "1099-R", "")
    doc["payload"] = {"recipient_name": "Eleanor Whitfield", "gross_distribution": 1000, "taxable_amount": 1000, "taxable_not_determined": None, "ira_sep_simple": False, "federal_withheld": 0, "tax_year": "2025"}
    _, _, out = run_prefilled("unknown-box", [doc], {**case1_intake, "documents_declared": ["1099-R"]})
    assert out["extracted"]["uncertain-r"]["fields"]["taxable_not_determined"] is None
    assert out["status"] == "waiting_human" and not out["lines"]
    assert any(e["kind"] == "uncertain_fact" for e in out["escalations"])


def test_model_confidence_cannot_override_explicit_uncertainty():
    from silverfile.agents.intake import apply
    guessed = {"intent": "answer", "updates": {"value": False}, "_text": "I am not sure"}
    facts = apply("can_be_claimed", guessed, {}, [])
    assert facts["can_be_claimed_as_dependent"] is None
    guessed_status = {"intent": "answer", "updates": {"filing_status": "single"}, "_text": "I don't know"}
    facts = apply("filing_status", guessed_status, {}, [])
    assert not facts.get("filing_status") and facts["_filing_status_unsure"] is True
