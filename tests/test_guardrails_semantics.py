"""Negation and uncertainty must survive scripted and model-assisted parsing."""
from __future__ import annotations

import pytest

from silverfile import service
from silverfile.agents.intake import apply, answering_party_from, parse_money, parse_yes_no, scripted_parse, stated_authority
from silverfile.security import auth
from silverfile.security.validate import unknown_fact_actions


@pytest.mark.parametrize("answer", ["No, I am not legally blind.", "I am not blind.", "No, that's not right.", "I don't have any.", "Nope."])
def test_negative_answer_is_not_a_positive_fragment(answer):
    assert parse_yes_no(answer) is False
    facts = apply("blind", {"_text": answer, "intent": "answer", "updates": {"value": True}}, {}, [])
    assert facts["taxpayer"]["blind"] is False


@pytest.mark.parametrize("answer", ["Yes, I am legally blind.", "Yes", "That's right."])
def test_clear_positive_answers_still_work(answer):
    assert parse_yes_no(answer) is True


@pytest.mark.parametrize("answer", ["Yes, no, I am not blind.", "I don’t know", "I cannot say", "Maybe not"])
def test_ambiguous_and_uncertain_boolean_answers_stay_unknown(answer):
    assert parse_yes_no(answer) is None
    facts = apply("blind", {"_text": answer, "intent": "answer", "updates": {"value": True}}, {}, [])
    assert facts["taxpayer"]["blind"] is None and "taxpayer.blind" in facts["_unknown_fields"]


@pytest.mark.parametrize("answer", [
    "I'm her daughter and I do not have permission.", "I'm her daughter without power of attorney.",
    "I'm her daughter, not authorized to do this.", "I'm her daughter and her permission was revoked.",
    "I'm her daughter and I need permission.", "I'm her daughter and I'm not sure I have permission.",
    "I'm her daughter and I don’t have permission.",
])
def test_authority_keywords_cannot_override_denial_or_uncertainty(answer):
    assert not stated_authority(answer)
    assert answering_party_from(answer)["answering_party"] == "third_party_no_authority"


def test_positive_authority_claim_is_still_noted():
    answer = "I'm her daughter and I hold power of attorney."
    assert stated_authority(answer)
    assert answering_party_from(answer)["answering_party"] == "third_party_with_authority"


@pytest.mark.parametrize("slot, field", [("other_income", "other_income_sources"), ("documents_declared", "documents_declared"), ("spouse_died_in_year", "spouse_died_in_year")])
def test_additional_unknown_facts_do_not_become_empty_or_false(slot, field):
    text = "I can't remember."
    parsed = {**scripted_parse(slot, text, {}), "_text": text}
    facts = apply(slot, parsed, {}, [])
    assert facts[field] is None and field in facts["_unknown_fields"]
    assert any(action["field"] == field for action in unknown_fact_actions({"intake": facts}))


def test_unknown_additional_dependent_preserves_the_previous_people():
    previous = [{"name": "Synthetic Dependent", "relationship": "grandson"}]
    facts = apply("more_dependents", {"_text": "I don't know", "intent": "unsure", "updates": {}}, {"dependents": previous}, [])
    assert facts["dependents"] == previous and "dependents" in facts["_unknown_fields"]


def test_negated_clause_does_not_hide_a_scope_signal():
    text = "No wages, but I had rental income."
    facts = apply("other_income", {"_text": text, "intent": "answer", "updates": {"other": []}}, {}, [])
    assert facts["other_income_sources"] == [text]


@pytest.mark.parametrize("text", ["No, I paid 500", "500 or 700", "-500"])
def test_conflicting_or_invalid_amounts_are_not_zero_or_a_guessed_value(text):
    assert parse_money(text) is None


def test_initial_and_mid_conversation_denials_pause(data_dir):
    for initial in (True, False):
        rid = service.create_return([], None, actor=auth.system("test"))
        service.run(rid)
        taxpayer = auth.taxpayer_principal(rid)
        if not initial:
            service.answer(rid, "Yes", actor=taxpayer)
        state = service.answer(rid, "I'm her daughter and I do not have permission.", actor=taxpayer)
        assert state["intake"]["answering_party"] == "third_party_no_authority"
        assert service.pending(rid)["type"] == "escalation"
        assert any(e["kind"] == "exploitation" and e["status"] == "open" for e in state["escalations"])


def test_unknown_other_income_pauses_and_staff_can_confirm_none(data_dir):
    facts = service.fixture_intake("case1_clean")
    facts = apply("other_income", {"_text": "I cannot remember", "intent": "answer", "updates": {"other": []}}, facts, [])
    system = auth.system("test")
    rid = service.create_return(service.fixture_documents("case1_clean"), facts, actor=system)
    state = service.run(rid)
    assert state["status"] == "waiting_human" and not state["lines"]
    state = service.resolve(rid, {"action": "continue"}, actor=system)
    assert state["hitl_log"][-1]["result"] == "no"
    state = service.resolve(rid, {"action": "continue", "note": "Preparer confirmed no other income from evidence.", "facts_patch": {"other_income_sources": []}}, actor=system)
    assert state["status"] == "complete" and not state["intake"]["_unknown_fields"]


def test_a_confident_model_cannot_grant_explicitly_denied_authority(data_dir, monkeypatch):
    from silverfile.agents import intake
    monkeypatch.setattr(intake.llm, "available", lambda: True)
    monkeypatch.setattr(intake, "llm_parse", lambda *args: ({"intent": "answer", "updates": {"answering_party": "third_party_with_authority"}}, 0, 0))
    rid = service.create_return([], None, actor=auth.system("test"))
    service.run(rid)
    state = service.answer(rid, "I'm her daughter and I do not have permission.", actor=auth.taxpayer_principal(rid))
    assert state["intake"]["answering_party"] == "third_party_no_authority"
    assert service.pending(rid)["escalation"]["kind"] == "exploitation"


@pytest.mark.parametrize("text", ["No, I paid 500", "500 or 700", "-500"])
def test_a_model_cannot_resolve_ambiguous_amounts_by_guessing(text):
    facts = apply("estimated_payments", {"_text": text, "intent": "answer", "updates": {"amount": 500}}, {}, [])
    assert facts["estimated_payments"] is None and "estimated_payments" in facts["_unknown_fields"]
