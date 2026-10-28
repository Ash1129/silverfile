"""The SilverFile inter-agent message protocol.

Every exchange between agents is an `AgentMessage` envelope on the `mailbox`
bus in graph state. The vocabulary is a small subset of FIPA-ACL
performatives, which is enough for a hub-and-spoke system:

  request   orchestrator -> worker      "your turn; here is your task and your open work orders"
  inform    worker -> orchestrator      "done; here is a summary of what I wrote"
  revise    review -> worker            a work order: one finding, addressed to the agent that owns it
  done      worker -> review            the work order was acted on (in_reply_to the revise)
  escalate  agent -> human              an escalation was raised (mirrors the Escalation record)
  resolve   human -> orchestrator       a Kushim employee resolved it
  refuse    worker -> orchestrator      the worker cannot act on the request
  violation orchestrator -> human       a contract or channel breach, kept for the audit
  ask       intake -> taxpayer          one question was asked (slot only, never the text)
  answer    taxpayer -> intake          the taxpayer answered (slot only, never the text)

`CHANNELS` is the policy: which sender may use which performative towards
which recipient. It encodes the brief's rules as data. Only Intake can
address the taxpayer, only Review can issue work orders, and workers never
address each other except through the work-order/done pair, which the
orchestrator routes. `make_message` refuses anything off-policy.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from ..provenance import now_iso
from ..state import AgentMessage

WORKERS = ("intake", "extraction", "eligibility", "computation", "review", "escalation", "finalize")
REVISABLE = ("computation", "extraction", "eligibility", "intake")  # agents a review work order can go to
PARTIES = ("orchestrator", *WORKERS, "human", "taxpayer")
PERFORMATIVES = ("request", "inform", "revise", "done", "escalate", "resolve", "refuse", "violation", "ask", "answer")

# topic -> required body keys
TOPICS: dict[str, tuple[str, ...]] = {
    "task": ("goal",),
    "result": ("summary",),
    "document.extracted": ("document_id", "kind", "issues"),
    "work_order": ("finding_id", "check", "severity", "detail"),
    "work_order.done": ("finding_id",),
    "escalation": ("escalation_id", "kind", "reason"),
    "resolution": ("escalation_id", "action"),
    "contract": ("agent", "rule", "detail"),
    "no_reply": ("request_id", "agent"),
    "question": ("slot",),
}


def _channels() -> dict[tuple[str, str], frozenset[str]]:
    c: dict[tuple[str, str], frozenset[str]] = {("orchestrator", "request"): frozenset(WORKERS)}
    for w in WORKERS:
        c[(w, "inform")] = frozenset({"orchestrator"})
        c[(w, "refuse")] = frozenset({"orchestrator"})
    c[("review", "revise")] = frozenset(REVISABLE)
    for w in REVISABLE:
        c[(w, "done")] = frozenset({"review"})
    for w in ("intake", "eligibility", "review", "orchestrator"):
        c[(w, "escalate")] = frozenset({"human"})
    c[("human", "resolve")] = frozenset({"orchestrator"})
    c[("orchestrator", "violation")] = frozenset({"human"})
    c[("intake", "ask")] = frozenset({"taxpayer"})
    c[("taxpayer", "answer")] = frozenset({"intake"})
    return c


CHANNELS = _channels()

SSN_RE = re.compile(r"\b(\d{3})-?(\d{2})-?(\d{4})\b")


class ProtocolError(ValueError):
    """A message that breaks the envelope schema or the channel policy."""


def allowed(sender: str, performative: str, recipient: str) -> bool:
    return recipient in CHANNELS.get((sender, performative), frozenset())


def scrub(obj: Any) -> Any:
    """Messages are an audit trail other agents read: no full SSN ever travels on the bus."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return SSN_RE.sub(lambda m: f"***-**-{m.group(3)}", obj)
    return obj


def validate(msg: dict[str, Any]) -> list[str]:
    """Problems with a message, empty if it is well formed and on-policy."""
    problems = []
    for key in ("id", "seq", "thread", "sender", "recipient", "performative", "topic", "body", "at"):
        if key not in msg:
            problems.append(f"missing field {key!r}")
    if problems:
        return problems
    if msg["sender"] not in PARTIES:
        problems.append(f"unknown sender {msg['sender']!r}")
    if msg["recipient"] not in PARTIES:
        problems.append(f"unknown recipient {msg['recipient']!r}")
    if msg["performative"] not in PERFORMATIVES:
        problems.append(f"unknown performative {msg['performative']!r}")
    if msg["topic"] not in TOPICS:
        problems.append(f"unknown topic {msg['topic']!r}")
    elif not isinstance(msg["body"], dict):
        problems.append("body must be an object")
    else:
        missing = [k for k in TOPICS[msg["topic"]] if k not in msg["body"]]
        if missing:
            problems.append(f"topic {msg['topic']!r} needs body keys {missing}")
    if not problems and not allowed(msg["sender"], msg["performative"], msg["recipient"]):
        problems.append(f"{msg['sender']} may not send {msg['performative']!r} to {msg['recipient']}")
    if msg["performative"] in ("inform", "done", "refuse", "resolve") and not msg.get("in_reply_to") and msg["topic"] != "document.extracted":  # branch results are partial; the aggregate replies
        problems.append(f"a {msg['performative']!r} must reply to an earlier message")
    return problems


def message_id(seq: int, sender: str, recipient: str, performative: str, topic: str, in_reply_to: str | None, body: dict[str, Any]) -> str:
    raw = json.dumps([seq, sender, recipient, performative, topic, in_reply_to, body], sort_keys=True, default=str)
    return "m-" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def make_message(seq: int, sender: str, recipient: str, performative: str, topic: str, body: dict[str, Any], *, in_reply_to: str | None = None, thread: str | None = None) -> AgentMessage:
    """Build, scrub and validate one envelope. Raises ProtocolError when off-policy."""
    body = scrub(dict(body))
    mid = message_id(seq, sender, recipient, performative, topic, in_reply_to, body)
    msg = AgentMessage(id=mid, seq=int(seq), thread=thread or mid, in_reply_to=in_reply_to, sender=sender, recipient=recipient, performative=performative, topic=topic, body=body, at=now_iso())
    problems = validate(msg)
    if problems:
        raise ProtocolError("; ".join(problems))
    return msg


def next_seq(messages: Iterable[AgentMessage] | None) -> int:
    return 1 + max((int(m.get("seq", 0)) for m in messages or []), default=0)
