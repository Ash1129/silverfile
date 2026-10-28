"""Inter-agent communication: the envelope, the channel policy, the write
contracts, the order-independent bus reducer, and the message flows through
the graph (handoffs, parallel extraction, review work orders, HITL, loop guard)."""
from __future__ import annotations

import itertools

import pytest

from conftest import CASE1, CASE2, case1_document_list, case2_document_list, load_intake, resolve, run_prefilled
from silverfile.agents.orchestrator import decide_next, orchestrator_node
from silverfile.comms import (CHANNELS, ProtocolError, allowed, communicating, enforce_write_contract, latest_request, make_message, open_work_orders,
                              thread, unanswered_requests, validate)
from silverfile.comms.bus import ContractViolation
from silverfile.state import LOOP_PATTERN_REPEATS, merge_messages


def _kinds(out, performative=None, topic=None):
    return [m for m in out["mailbox"] if (performative is None or m["performative"] == performative) and (topic is None or m["topic"] == topic)]


@pytest.fixture(scope="module")
def clean():
    _, _, out = run_prefilled("comms-clean", case1_document_list(), load_intake(CASE1))
    assert out["status"] == "complete"
    return out


# ---------------------------------------------------------------- envelope and policy


def test_a_well_formed_request_validates():
    m = make_message(1, "orchestrator", "computation", "request", "task", {"goal": "compute"})
    assert validate(m) == []
    assert m["thread"] == m["id"] and m["id"].startswith("m-")


def test_only_intake_may_address_the_taxpayer():
    assert allowed("intake", "ask", "taxpayer")
    for agent in ("orchestrator", "extraction", "eligibility", "computation", "review", "escalation"):
        for perf in ("ask", "inform", "request", "revise"):
            assert not allowed(agent, perf, "taxpayer"), (agent, perf)
    with pytest.raises(ProtocolError, match="computation may not send 'ask' to taxpayer"):
        make_message(1, "computation", "taxpayer", "ask", "question", {"slot": "dob"})


def test_workers_do_not_talk_to_each_other_except_by_work_order():
    assert allowed("review", "revise", "computation")
    assert allowed("computation", "done", "review")
    assert not allowed("computation", "inform", "eligibility")
    assert not allowed("eligibility", "request", "computation")  # only the hub hands off
    assert not allowed("computation", "revise", "review")  # only Review issues work orders
    assert not allowed("review", "revise", "review")
    # every request comes from the orchestrator, every result goes back to it
    assert {s for (s, p) in CHANNELS if p == "request"} == {"orchestrator"}
    assert all(r == {"orchestrator"} for (s, p), r in CHANNELS.items() if p == "inform")


def test_only_a_person_resolves_an_escalation():
    assert allowed("human", "resolve", "orchestrator")
    assert not any(allowed(a, "resolve", "orchestrator") for a in ("review", "escalation", "eligibility"))


@pytest.mark.parametrize("args,match", [
    (("orchestrator", "review", "request", "task", {}), "needs body keys"),
    (("orchestrator", "review", "request", "nonsense", {"goal": "x"}), "unknown topic"),
    (("computation", "orchestrator", "inform", "result", {"summary": "x"}), "must reply"),
    (("ghost", "orchestrator", "inform", "result", {"summary": "x"}), "unknown sender"),
])
def test_malformed_messages_are_refused(args, match):
    with pytest.raises(ProtocolError, match=match):
        make_message(1, *args)


def test_no_full_ssn_travels_on_the_bus():
    m = make_message(1, "review", "computation", "revise", "work_order", {"finding_id": "f", "check": "c", "severity": "high", "detail": "dependent 901-23-4567 looks wrong", "nested": ["901234567"]})
    assert "901-23-4567" not in str(m) and "901234567" not in str(m)
    assert "***-**-4567" in m["body"]["detail"]


# ---------------------------------------------------------------- reducer


def test_bus_merge_is_order_independent_and_idempotent():
    req = make_message(1, "orchestrator", "extraction", "request", "task", {"goal": "x"})
    branches = [make_message(2, "extraction", "orchestrator", "inform", "document.extracted", {"document_id": d, "kind": "W-2", "issues": 0}, thread=req["thread"]) for d in ("a", "b", "c", "d")]
    results = set()
    for perm in itertools.permutations(branches):
        acc = merge_messages([], [req])
        for m in perm:  # each parallel branch writes separately, in any finishing order
            acc = merge_messages(acc, [m])
        results.add(tuple(m["id"] for m in acc))
    assert len(results) == 1
    merged = list(results)[0]
    assert merged[0] == req["id"]
    assert merge_messages(merge_messages([], [req, *branches]), [branches[0], req]) == merge_messages([], [req, *branches])  # redelivery is a no-op


# ---------------------------------------------------------------- write contracts


def test_review_cannot_write_lines():
    clean_, illegal = enforce_write_contract("review", {"review_passed": False, "lines": {"16": 0.0}, "provenance": {}})
    assert illegal == ["lines", "provenance"] and "lines" not in clean_


def test_the_hub_drops_an_illegal_write_and_records_a_violation(clean, monkeypatch):
    monkeypatch.delenv("SILVERFILE_STRICT_CONTRACTS", raising=False)
    state = {**clean, "mailbox": [*clean["mailbox"], make_message(99, "orchestrator", "review", "request", "task", {"goal": "review"})]}
    sneaky = communicating("review", lambda s: {"review_round": 9, "lines": {"16": 0.0}, "events": ["review: tampered"]})
    update = sneaky(state)
    assert "lines" not in update and update["review_round"] == 9
    v = [m for m in update["mailbox"] if m["performative"] == "violation"]
    assert len(v) == 1 and v[0]["body"]["agent"] == "review" and "'lines'" in v[0]["body"]["detail"]
    assert any(e.startswith("contract: review tried to write ['lines']") for e in update["events"])
    monkeypatch.setenv("SILVERFILE_STRICT_CONTRACTS", "1")
    with pytest.raises(ContractViolation):
        sneaky(state)


def test_every_agent_stays_inside_its_contract_on_real_runs(clean):
    assert _kinds(clean, "violation") == []


# ---------------------------------------------------------------- flows through the graph


def test_every_handoff_is_a_request_answered_on_its_thread(clean):
    requests = _kinds(clean, "request")
    assert [r["recipient"] for r in requests] == ["extraction", "eligibility", "computation", "review", "finalize"]
    assert unanswered_requests(clean) == []
    for r in requests:
        replies = [m for m in thread(clean, r["thread"]) if m["in_reply_to"] == r["id"]]
        assert len(replies) == 1 and replies[0]["performative"] == "inform" and replies[0]["sender"] == r["recipient"]
        assert replies[0]["seq"] > r["seq"]
    assert all(m["sender"] == "orchestrator" or m["recipient"] in ("orchestrator", "review", "human", "taxpayer", "intake") for m in clean["mailbox"])


def test_parallel_extraction_posts_one_result_per_document_at_the_same_time(clean):
    docs = _kinds(clean, topic="document.extracted")
    assert sorted(d["body"]["document_id"] for d in docs) == ["int-1", "r-1", "ssa-1", "w2-1"]
    assert len({d["seq"] for d in docs}) == 1  # one superstep
    req = _kinds(clean, "request")[0]
    assert all(d["thread"] == req["thread"] and d["body"]["request_id"] == req["id"] for d in docs)
    agg = next(m for m in clean["mailbox"] if m["in_reply_to"] == req["id"])
    assert agg["seq"] > docs[0]["seq"] and "aggregated 4" in agg["body"]["summary"]


def test_requests_carry_the_read_contract(clean):
    comp = next(m for m in _kinds(clean, "request") if m["recipient"] == "computation")
    assert set(comp["body"]["reads"]) == {"intake", "eligibility", "extracted", "computation_version"}
    assert "messages" not in comp["body"]["reads"]  # computation never sees the conversation


def test_review_work_order_round_trip():
    _, _, out = run_prefilled("comms-wo", case1_document_list(), load_intake(CASE1), fault_injection={"drop_provenance_for": "6b", "rounds": 1})
    assert out["status"] == "complete"
    (order,) = _kinds(out, "revise")
    assert order["sender"] == "review" and order["recipient"] == "computation" and order["body"]["line"] == "6b"
    assert order["thread"] == "review-round-1"
    # the next request to computation forwards the order; computation's reply closes it
    fwd = [m for m in _kinds(out, "request") if m["recipient"] == "computation"][1]
    assert fwd["body"]["work_orders"][0]["id"] == order["id"]
    (done,) = _kinds(out, "done")
    assert done["in_reply_to"] == order["id"] and done["body"]["acted_in"] == fwd["id"]
    assert open_work_orders(out) == []
    assert any(n.startswith("re-run for review work order provenance-missing:6b") for n in out["computation_notes"])


def test_open_work_orders_route_each_addressee_before_review_again():
    order = make_message(10, "review", "computation", "revise", "work_order", {"finding_id": "f1", "check": "c", "severity": "high", "detail": "d"})
    state = {"intake_complete": True, "extraction_done": True, "eligibility": {"eligible_1040sr": True, "in_scope": True, "chart_applies": True},
             "lines": {"1z": 1.0}, "review_round": 1, "review_passed": False, "revise_target": None, "handoffs": [], "mailbox": [order]}
    assert decide_next(state) == "computation"
    done = make_message(11, "computation", "review", "done", "work_order.done", {"finding_id": "f1"}, in_reply_to=order["id"])
    assert decide_next({**state, "mailbox": [order, done]}) == "review"
    # a passed or accepted review is not reopened by a stale order
    assert decide_next({**state, "review_passed": True}) == "finalize"


def test_hitl_escalation_is_an_escalate_answered_by_a_resolve():
    g, cfg, out = run_prefilled("comms-hitl", case2_document_list(), load_intake(CASE2))
    (esc,) = _kinds(out, "escalate")
    assert esc["sender"] == "eligibility" and esc["recipient"] == "human" and esc["body"]["kind"] == "filing_status"
    assert latest_request(out, "escalation") is not None  # the hub's handoff waits with the interrupt
    final = resolve(g, cfg, "continue", note="MFJ", facts_patch={"filing_status": "mfj"})
    (res,) = _kinds(final, "resolve")
    assert res["in_reply_to"] == esc["id"] and res["thread"] == esc["thread"] and res["body"]["action"] == "continue"
    assert final["status"] == "complete" and unanswered_requests(final) == []


def test_loop_guard_escalation_is_posted_by_the_orchestrator():
    g, cfg, out = run_prefilled("comms-loop", case1_document_list(), load_intake(CASE1), fault_injection={"ping_pong": True})
    (esc,) = _kinds(out, "escalate")
    assert esc["sender"] == "orchestrator" and esc["body"]["kind"] == "loop_guard"
    reqs = [m["recipient"] for m in _kinds(out, "request")]
    assert reqs[: 2 * LOOP_PATTERN_REPEATS] == ["eligibility", "computation"] * LOOP_PATTERN_REPEATS


def test_an_unanswered_request_is_flagged_once():
    req = make_message(1, "orchestrator", "eligibility", "request", "task", {"goal": "x"})
    state = {"intake_complete": False, "handoffs": [], "mailbox": [req]}
    first = orchestrator_node(state)
    flags = [m for m in first["mailbox"] if m["topic"] == "no_reply"]
    assert len(flags) == 1 and flags[0]["body"] == {"request_id": req["id"], "agent": "eligibility"}
    again = orchestrator_node({**state, "mailbox": merge_messages(state["mailbox"], first["mailbox"])})
    assert not [m for m in again["mailbox"] if m["topic"] == "no_reply" and m["body"]["request_id"] == req["id"]]


def test_intake_turns_are_logged_without_the_words():
    from test_intake import _drive  # the scripted-conversation driver
    import json
    from conftest import new_graph
    from silverfile.graph import new_return_state
    answers = json.loads((CASE1 / "answers.json").read_text())
    g = new_graph()
    cfg = {"configurable": {"thread_id": "comms-chat"}}
    g.invoke(new_return_state("comms-chat"), cfg)
    _drive(g, cfg, answers, case1_document_list())
    state = g.get_state(cfg).values
    asks, ans = _kinds(state, "ask"), _kinds(state, "answer")
    assert len(asks) == len(ans) == len(answers)
    assert all(set(m["body"]) == {"slot"} for m in [*asks, *ans])  # the slot, never the text
    assert [a["in_reply_to"] for a in ans] == [q["id"] for q in asks]
    assert state["status"] == "complete" and unanswered_requests(state) == []
