"""Agent memory: resuming after a break, the answer history, and the context window.

Covers src/silverfile/memory.py and where the agents use it:
- a session abandoned on Tuesday and resumed on Friday, in a new process,
  shows a recap in front of the question that was waiting;
- an answer the taxpayer corrects or goes back to is kept with its old value,
  never with a Social Security number in it;
- the context window stays flat with the strategy on and grows without it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from langgraph.types import Command

from conftest import interrupts
from silverfile import memory, service
from silverfile.dashboard import web as webmod
from silverfile.security import auth
from silverfile.graph import compile_graph, get_checkpointer, new_return_state
from silverfile.memory import ContextWindow, record_answer_changes, resume_recap, session_summary, with_resume_recap
from test_intake_dignity import MARRIED, SINGLE, SPOUSE_TEST_SSN, TEST_SSN, converse
from webhelpers import answer, login, start_taxpayer

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)  # a Friday
TUESDAY = (NOW - timedelta(days=3)).isoformat()


# ---------------------------------------------------------------- resuming after a break
def test_no_recap_without_a_break():
    facts = {"taxpayer": {"first_name": "Eleanor", "last_name": "Whitfield"}, "_last_activity_at": (NOW - timedelta(minutes=5)).isoformat()}
    assert resume_recap(facts, NOW) is None
    assert resume_recap({}, NOW) is None  # nothing answered yet: nothing to recap


def test_recap_after_a_break_says_what_was_covered():
    facts = {"taxpayer": {"first_name": "Eleanor", "last_name": "Whitfield", "dob": "1950-03-14"}, "filing_status": "single", "_last_activity_at": TUESDAY}
    recap = resume_recap(facts, NOW)
    assert recap.startswith("Welcome back.")
    assert "Eleanor Whitfield" in recap and "1950-03-14" in recap and "single" in recap


def test_the_waiting_question_gets_the_recap_once_and_is_not_changed():
    state = {"intake": {"taxpayer": {"first_name": "Eleanor", "last_name": "Whitfield"}, "_last_activity_at": TUESDAY}}
    payload = {"type": "question", "slot": "dob", "text": "What is your date of birth?"}
    shown = with_resume_recap(payload, state, NOW)
    assert shown["text"].startswith("Welcome back.") and shown["text"].endswith("What is your date of birth?")
    assert payload["text"] == "What is your date of birth?"  # the checkpointed question is left alone
    assert with_resume_recap(shown, state, NOW)["text"].count("Welcome back.") == 1


def test_escalations_and_the_first_question_get_no_recap():
    state = {"intake": {"taxpayer": {"first_name": "Eleanor"}, "_last_activity_at": TUESDAY}}
    escalation = {"type": "escalation", "escalation": {"kind": "advice"}}
    assert with_resume_recap(escalation, state, NOW) is escalation  # staff see the item as raised
    first = {"type": "question", "slot": "answering_party", "text": "Are you the taxpayer?"}
    assert with_resume_recap(first, state, NOW) is first
    assert with_resume_recap(None, state, NOW) is None


def test_a_session_left_on_tuesday_resumes_on_friday_in_a_new_process(tmp_path):
    db = tmp_path / "checkpoints.sqlite"
    cfg = {"configurable": {"thread_id": "resume-test"}}
    g = compile_graph(get_checkpointer(db))
    g.invoke(new_return_state("resume-test", []), cfg)
    for slot in ("answering_party", "name", "ssn", "dob"):
        (pending,) = interrupts(g, cfg)
        assert pending["slot"] == slot
        g.invoke(Command(resume=SINGLE[slot]), cfg)
    (waiting,) = interrupts(g, cfg)
    del g  # the process ends with a question waiting

    g = compile_graph(get_checkpointer(db))  # Friday: a new process, the same file
    snap = g.get_state(cfg)
    (again,) = [i.value for t in snap.tasks for i in t.interrupts]
    assert again["slot"] == waiting["slot"]  # the same question, not the start
    friday = datetime.fromisoformat(snap.values["intake"]["_last_activity_at"]) + timedelta(days=3)
    shown = with_resume_recap(again, snap.values, friday)
    assert shown["text"].startswith("Welcome back.") and "Eleanor Whitfield" in shown["text"] and "1950-03-14" in shown["text"]
    assert shown["text"].endswith(waiting["text"])

    g.invoke(Command(resume=SINGLE[again["slot"]]), cfg)  # and the conversation carries on
    facts = g.get_state(cfg).values["intake"]
    assert facts["taxpayer"]["last_name"] == "Whitfield" and facts["taxpayer"]["dob"] == "1950-03-14"


# ---------------------------------------------------------------- earlier answers are never lost
def test_going_back_keeps_the_earlier_answer():
    state, _ = converse(SINGLE, extra={"confirm": ["No, my date of birth is wrong"], "dob": []})
    dob = [h for h in memory.answer_history(state) if h["field"] == "taxpayer.dob"]
    assert [h["how"] for h in dob] == ["answered", "went_back", "answered"]
    assert dob[1]["old"] == "1950-03-14" and dob[1]["new"] is None


def test_a_corrected_answer_keeps_the_old_value():
    before = {"taxpayer": {"dob": "1950-03-14"}, "estimated_payments": 500.0}
    after = {"taxpayer": {"dob": "1954-03-14"}, "estimated_payments": 500.0}
    out = record_answer_changes(before, after, slot="dob", at="2026-10-30T15:00:00+00:00")
    assert out["_answer_history"] == [{"at": "2026-10-30T15:00:00+00:00", "slot": "dob", "field": "taxpayer.dob", "old": "1950-03-14", "new": "1954-03-14", "how": "corrected"}]


def test_a_kushim_change_is_recorded_as_one():
    out = record_answer_changes({"filing_status": None}, {"filing_status": "qss"}, slot="filing_status", at="t", how="kushim")
    assert out["_answer_history"][0]["how"] == "kushim"


def test_nothing_changed_adds_nothing():
    facts = {"taxpayer": {"dob": "1950-03-14"}}
    assert "_answer_history" not in record_answer_changes(facts, json.loads(json.dumps(facts)), slot="dob", at="t")


def test_the_history_never_holds_a_social_security_number():
    state, _ = converse(MARRIED, extra={"confirm": ["No, my social security number is wrong"]})
    history = memory.answer_history(state)
    assert any(h["field"] == "taxpayer.ssn" for h in history)
    dumped = json.dumps(history)
    for ssn in (TEST_SSN, SPOUSE_TEST_SSN):
        assert ssn not in dumped and ssn.replace("-", "") not in dumped


def test_bookkeeping_is_not_an_answer():
    before = {"_turns": 1, "noted_questions": [], "taxpayer": {}}
    after = {"_turns": 2, "noted_questions": ["Should I convert my IRA?"], "_last_activity_at": "t", "taxpayer": {}}
    assert "_answer_history" not in record_answer_changes(before, after, slot="estimated_payments", at="t")


def test_the_history_is_bounded():
    facts: dict = {}
    for i in range(memory.MAX_HISTORY + 20):
        facts = record_answer_changes(facts, {**facts, "estimated_payments": float(i)}, slot="estimated_payments", at=str(i))
    assert len(facts["_answer_history"]) == memory.MAX_HISTORY
    assert facts["_answer_history"][-1]["new"] == float(memory.MAX_HISTORY + 19)  # the newest are kept


def test_the_summary_mentions_changed_answers():
    state, _ = converse(SINGLE, extra={"confirm": ["No, my date of birth is wrong"]})
    assert "earlier answer(s) changed" in session_summary(state)
    plain, _ = converse(SINGLE)
    assert "changed" not in session_summary(plain)


def test_the_dashboard_shows_the_recap_to_the_taxpayer_and_the_change_to_staff(web, monkeypatch):
    tp = TestClient(web)
    start_taxpayer(tp)
    for text in ("Yes, it's my return", "Eleanor Whitfield", TEST_SSN, "3/14/1950", "Actually I need to change my date of birth", "3/14/1951"):
        assert answer(tp, text).status_code == 303
    page = tp.get("/t").text
    assert "Welcome back" not in page  # no break yet

    monkeypatch.setattr(memory, "RESUME_AFTER_MINUTES", -1)  # every gap now counts as a break
    page = tp.get("/t").text
    assert "Welcome back. Here&#39;s where we were:" in page and "1951-03-14" in page

    rid = auth.load_session(tp.cookies[webmod.TAXPAYER_COOKIE], "taxpayer").subject
    assert "Welcome back" not in service.pending(rid)["text"]  # staff see the question as it was asked
    staff = TestClient(web)
    login(staff, "memory-preparer")
    html = staff.get(f"/returns/{rid}").text
    assert "Changed answers" in html and "taxpayer went back" in html and "1950-03-14" in html


# ---------------------------------------------------------------- the context window (section 4.5)
def _run(strategy: str, n: int = 10) -> list[int]:
    window = ContextWindow(strategy=strategy)
    sizes = []
    for i in range(n):
        item = f"W-2 number {i}: " + "box text " * 50
        sizes.append(len(json.dumps(window.messages_for(item))))
        window.remember(item, '{"wages": 1000}', decision=f"w2_{i:02d}: wages 1000")
    return sizes


def test_without_a_strategy_the_prompt_grows_with_every_item():
    sizes = _run("none")
    assert all(b > a for a, b in zip(sizes, sizes[1:]))
    assert sizes[-1] > 8 * sizes[0]


def test_selective_recall_keeps_the_prompt_flat():
    sizes = _run("selective")
    assert sizes[-1] < 1.5 * sizes[0]
    assert sizes[-1] < _run("none")[-1] / 5


def test_old_decisions_fold_into_one_line():
    window = ContextWindow(strategy="selective", max_decisions=3)
    for i in range(7):
        window.remember(f"item {i}", "ok", decision=f"d{i}")
    assert len(window.decisions) == 4  # one folded line plus the three newest
    assert window.decisions[0].startswith("earlier:") and window.decisions[-1] == "d6"
    text = window.messages_for("item 7")[0]["content"]
    assert "d6" in text and "d0" not in text


def test_an_image_item_keeps_its_image_and_gets_the_recap():
    window = ContextWindow(strategy="selective")
    window.remember("first", "ok", decision="w2_01: wages 1000")
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
    (msg,) = window.messages_for([image, {"type": "text", "text": "Read this W-2."}])
    assert msg["content"][0] == image
    assert "w2_01: wages 1000" in msg["content"][-1]["text"] and "Read this W-2." in msg["content"][-1]["text"]
