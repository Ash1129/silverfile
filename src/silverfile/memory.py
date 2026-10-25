"""Agent memory: what persists, what is summarised, and how the model's context
is kept flat on long sessions.

Three kinds of memory:
1. Durable session state: every graph step is checkpointed (SqliteSaver in
   graph.py). A session abandoned on Tuesday resumes on Friday at the same
   question. Nothing is lost on process restart.
2. Working summary: `session_summary` turns the intake facts and progress into
   a short plain-language recap, used when a session resumes and shown in the
   dashboard. The orchestrator keeps the structured facts; the summary is for
   people and for prompts.
3. Model context strategy (section 4.5): `ContextWindow` builds the messages
   for the next model call. With the strategy on it uses selective recall: a
   rolling summary of the decisions so far plus the current item only. With it
   off it replays the whole history, which grows with every item. `run.py
   batch` measures both and writes docs/metrics.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agents.intake import summarize_facts
from .state import ReturnState


def session_summary(state: ReturnState) -> str:
    facts = state.get("intake") or {}
    parts = [summarize_facts(facts)]
    n_docs = len(state.get("extracted") or {})
    if n_docs:
        parts.append(f"{n_docs} document(s) read.")
    if state.get("lines"):
        parts.append(f"Draft computed (version {state.get('computation_version')}); review {'passed' if state.get('review_passed') else 'pending'} after {state.get('review_round', 0)} round(s).")
    open_ = [e for e in state.get("escalations", []) if e.get("status") == "open"]
    if open_:
        parts.append(f"Waiting on a Kushim employee for: {', '.join(e['kind'] for e in open_)}.")
    if state.get("status") == "complete":
        parts.append("The draft is complete and ready for the preparer.")
    return " ".join(parts)


@dataclass
class ContextWindow:
    """Builds the message list for the next model call in a long session.

    strategy="selective": summary of decisions so far + the current item.
    strategy="none": every previous item and reply, verbatim (the baseline).
    """

    strategy: str = "selective"
    history: list[dict[str, str]] = field(default_factory=list)  # {"item": ..., "reply": ...}
    decisions: list[str] = field(default_factory=list)
    max_decisions: int = 12

    def messages_for(self, current_item: str) -> list[dict[str, Any]]:
        if self.strategy == "none":
            msgs: list[dict[str, Any]] = []
            for h in self.history:
                msgs.append({"role": "user", "content": h["item"]})
                msgs.append({"role": "assistant", "content": h["reply"]})
            msgs.append({"role": "user", "content": current_item})
            return msgs
        recap = "Decisions so far: " + ("; ".join(self.decisions[-self.max_decisions :]) if self.decisions else "none yet") + "."
        return [{"role": "user", "content": f"{recap}\n\nCurrent item:\n{current_item}"}]

    def remember(self, item: str, reply: str, decision: str | None = None) -> None:
        self.history.append({"item": item, "reply": reply})
        if decision:
            self.decisions.append(decision)
        if self.strategy == "selective" and len(self.decisions) > self.max_decisions:
            # rolling summarisation: fold the oldest decisions into one line
            old, self.decisions = self.decisions[: -self.max_decisions], self.decisions[-self.max_decisions :]
            self.decisions.insert(0, f"earlier: {len(old)} items processed, all recorded")
