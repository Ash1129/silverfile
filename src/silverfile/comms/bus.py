"""The message bus: queries over the mailbox, and the node wrapper that makes
every agent speak the protocol.

`communicating(agent, fn)` wraps an agent node. The agent code stays a plain
`state -> update` function; the wrapper, acting for the hub,
  1. enforces the agent's write contract (illegal keys are dropped and a
     `violation` is posted; with SILVERFILE_STRICT_CONTRACTS=1 it raises),
  2. closes the work orders addressed to the agent with `done` replies,
  3. turns Review's failed findings into `revise` work orders,
  4. mirrors new and resolved escalations as `escalate` / `resolve`,
  5. replies `inform` to the orchestrator's `request`, on the same thread.
Extraction's parallel branches post `document.extracted` at a shared seq,
which the `merge_messages` reducer orders deterministically.
"""
from __future__ import annotations

import functools
import os
from typing import Any, Callable

from ..state import AgentMessage, ReturnState
from .contracts import GOALS, READS, enforce_write_contract
from .protocol import REVISABLE, make_message, next_seq

REPLIES = ("inform", "refuse")


class ContractViolation(RuntimeError):
    pass


# ---------------------------------------------------------------- queries


def mailbox(state: ReturnState) -> list[AgentMessage]:
    return list(state.get("mailbox") or [])


def inbox(state: ReturnState, agent: str, performative: str | None = None) -> list[AgentMessage]:
    return [m for m in mailbox(state) if m["recipient"] == agent and (performative is None or m["performative"] == performative)]


def _replied_to(state: ReturnState, performatives: tuple[str, ...]) -> set[str]:
    return {m["in_reply_to"] for m in mailbox(state) if m.get("in_reply_to") and m["performative"] in performatives}


def latest_request(state: ReturnState, agent: str) -> AgentMessage | None:
    """The orchestrator's most recent unanswered request to this agent."""
    answered = _replied_to(state, REPLIES)
    pending = [m for m in inbox(state, agent, "request") if m["id"] not in answered]
    return pending[-1] if pending else None


def unanswered_requests(state: ReturnState) -> list[AgentMessage]:
    answered = _replied_to(state, REPLIES)
    return [m for m in mailbox(state) if m["performative"] == "request" and m["id"] not in answered]


def open_work_orders(state: ReturnState, agent: str | None = None) -> list[AgentMessage]:
    """Review work orders that have not had a `done` reply yet."""
    done = _replied_to(state, ("done",))
    return [m for m in mailbox(state) if m["performative"] == "revise" and m["id"] not in done and (agent is None or m["recipient"] == agent)]


def thread(state: ReturnState, thread_id: str) -> list[AgentMessage]:
    return [m for m in mailbox(state) if m["thread"] == thread_id]


def escalate_message_for(state: ReturnState, escalation_id: str) -> AgentMessage | None:
    return next((m for m in mailbox(state) if m["performative"] == "escalate" and m["body"].get("escalation_id") == escalation_id), None)


# ---------------------------------------------------------------- orchestrator side


def request_for(state: ReturnState, agent: str, seq: int, **extra: Any) -> AgentMessage:
    """The orchestrator's handoff: goal, the context the agent reads, and its open work orders."""
    body: dict[str, Any] = {"goal": GOALS[agent], "reads": list(READS.get(agent, ())), **extra}
    if agent in REVISABLE:
        orders = open_work_orders(state, agent)
        if orders:
            body["work_orders"] = [{"id": o["id"], "finding_id": o["body"]["finding_id"], "detail": o["body"]["detail"]} for o in orders]
    if agent == "extraction":
        body["documents"] = [d.get("id") for d in state.get("documents_in") or []]
    return make_message(seq, "orchestrator", agent, "request", "task", body)


def escalate_message(seq: int, sender: str, esc: dict[str, Any], in_reply_to: str | None = None) -> AgentMessage:
    return make_message(seq, sender, "human", "escalate", "escalation", {"escalation_id": esc.get("id"), "kind": esc.get("kind"), "reason": esc.get("reason", "")}, in_reply_to=in_reply_to)


# ---------------------------------------------------------------- agent side


def _summary(agent: str, update: dict[str, Any]) -> dict[str, Any]:
    events = update.get("events") or []
    body: dict[str, Any] = {"summary": events[-1] if events else f"{agent}: done", "wrote": sorted(k for k in update if k not in ("events", "handoffs", "mailbox", "token_log"))}
    if agent == "computation":
        body["version"] = update.get("computation_version")
        body["lines"] = len(update.get("lines") or {})
    if agent == "review" and update.get("review_history"):
        rr = update["review_history"][-1]
        body.update(round=rr.get("round"), passed=rr.get("passed"), findings=len(rr.get("findings") or []), send_to=update.get("revise_target"))
    if agent == "eligibility":
        el = update.get("eligibility") or {}
        body.update(eligible=el.get("eligible_1040sr"), in_scope=el.get("in_scope"), box_count=el.get("box_count"))
    return body


def _asked_slot(update: dict[str, Any]) -> str | None:
    for ev in update.get("events") or []:
        if ev.startswith("intake: asked "):
            return ev[len("intake: asked "):].split(" ")[0]
    return None


def communicating(agent: str, fn: Callable[[ReturnState], dict[str, Any]]) -> Callable[[ReturnState], dict[str, Any]]:
    @functools.wraps(fn)
    def node(state: ReturnState) -> dict[str, Any]:
        raw = fn(state) or {}
        update, illegal = enforce_write_contract(agent, raw)
        if illegal and os.environ.get("SILVERFILE_STRICT_CONTRACTS") == "1":
            raise ContractViolation(f"{agent} may not write {illegal}")
        posted: list[AgentMessage] = list(update.get("mailbox") or [])
        seq = next_seq([*mailbox(state), *posted])
        out: list[AgentMessage] = []

        def post(*args: Any, **kw: Any) -> AgentMessage:
            nonlocal seq
            m = make_message(seq, *args, **kw)
            seq += 1
            out.append(m)
            return m

        req = latest_request(state, agent)
        for key in illegal:
            post("orchestrator", "human", "violation", "contract", {"agent": agent, "rule": "write contract", "detail": f"{agent} tried to write {key!r}; the update was dropped"}, thread=req["thread"] if req else None)

        if agent == "intake" and update.get("messages"):
            slot = _asked_slot(update) or str(state.get("intake_cursor") or "?")
            q = post("intake", "taxpayer", "ask", "question", {"slot": slot})
            post("taxpayer", "intake", "answer", "question", {"slot": slot}, in_reply_to=q["id"], thread=q["thread"])

        if agent in REVISABLE:
            for o in open_work_orders(state, agent):
                post(agent, "review", "done", "work_order.done", {"finding_id": o["body"]["finding_id"], "acted_in": (req or {}).get("id")}, in_reply_to=o["id"], thread=o["thread"])

        if agent == "review" and update.get("review_history"):
            rr = update["review_history"][-1]
            if not rr.get("passed"):
                for f in rr.get("findings") or []:
                    if f.get("send_to") in REVISABLE and f.get("severity") in ("high", "medium"):
                        post("review", f["send_to"], "revise", "work_order", {"finding_id": f.get("id"), "check": f.get("check"), "severity": f.get("severity"), "detail": f.get("detail", ""), "line": f.get("line"), "round": rr.get("round")}, thread=f"review-round-{rr.get('round')}")

        known = {m["body"].get("escalation_id") for m in mailbox(state) if m["performative"] == "escalate"}
        for esc in update.get("escalations") or []:
            if esc.get("status") == "open" and esc.get("id") not in known:
                out.append(escalate_message(seq, agent, esc))
                seq += 1
            elif esc.get("status") == "resolved":
                ask = escalate_message_for(state, esc.get("id", ""))
                post("human", "orchestrator", "resolve", "resolution", {"escalation_id": esc.get("id"), "action": (esc.get("resolution") or {}).get("action"), "resolved_by": esc.get("resolved_by")},
                     in_reply_to=ask["id"] if ask else (req or {}).get("id"), thread=ask["thread"] if ask else None)

        if req:
            post(agent, "orchestrator", "inform", "result", _summary(agent, update), in_reply_to=req["id"], thread=req["thread"])

        update["mailbox"] = [*posted, *out]
        if illegal:
            update["events"] = [*(update.get("events") or []), f"contract: {agent} tried to write {illegal}; dropped by the hub"]
        return update

    return node


def communicating_branch(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """For one extraction `Send` branch: the payload carries the request it answers."""
    @functools.wraps(fn)
    def node(payload: dict[str, Any]) -> dict[str, Any]:
        raw = fn(payload) or {}
        update, illegal = enforce_write_contract("extraction", raw)
        if illegal:
            raise ContractViolation(f"extraction branch may not write {illegal}")
        req = payload.get("request")
        for rec in (update.get("extracted") or {}).values():
            msg = make_message(int(req["seq"]) + 1 if req else 1, "extraction", "orchestrator", "inform", "document.extracted",
                               {"document_id": rec.get("id"), "kind": rec.get("kind"), "issues": len(rec.get("issues") or []), "method": rec.get("method"), "out_of_scope": bool(rec.get("out_of_scope")), "request_id": req["id"] if req else None},
                               thread=req["thread"] if req else None)  # a partial result: the aggregate step sends the reply
            update["mailbox"] = [*(update.get("mailbox") or []), msg]
        return update

    return node


# ---------------------------------------------------------------- dashboard view


def _detail(m: AgentMessage) -> str:
    b = m.get("body") or {}
    if m["performative"] == "request":
        orders = b.get("work_orders") or []
        return b.get("goal", "") + (f" (+{len(orders)} work order{'s' if len(orders) != 1 else ''})" if orders else "")
    if m["topic"] == "document.extracted":
        return f"{b.get('document_id')} read as {b.get('kind')} via {b.get('method')} ({b.get('issues')} issue(s))"
    if m["performative"] in ("revise",):
        return f"[{b.get('severity')}] {b.get('detail')}"
    if m["performative"] in ("ask", "answer"):
        return f"slot {b.get('slot')}"
    if m["performative"] == "escalate":
        return f"{b.get('kind')}: {b.get('reason')}"
    if m["performative"] == "resolve":
        return f"{b.get('action')} by {b.get('resolved_by')}"
    if m["performative"] == "done":
        return f"acted on {b.get('finding_id')}"
    if m["performative"] == "violation":
        return b.get("detail") or f"no reply from {b.get('agent')} to {b.get('request_id')}"
    return str(b.get("summary", ""))


def digest(state: ReturnState) -> dict[str, Any]:
    """Counts and display rows for the dashboard's message-log card."""
    msgs = mailbox(state)
    open_ = unanswered_requests(state)
    requests = [m for m in msgs if m["performative"] == "request"]
    # the newest request may legitimately be waiting (an interrupt for the taxpayer or a person)
    in_flight = open_[-1] if open_ and requests and open_[-1]["id"] == requests[-1]["id"] else None
    return {
        "count": len(msgs),
        "in_flight": in_flight["recipient"] if in_flight else None,
        "unanswered": len(open_) - (1 if in_flight else 0),
        "open_orders": len(open_work_orders(state)),
        "violations": sum(1 for m in msgs if m["performative"] == "violation"),
        "rows": [{**m, "detail": _detail(m)} for m in msgs],
    }
