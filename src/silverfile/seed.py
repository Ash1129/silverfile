"""Demo returns for a shared dashboard.

A fresh data directory shows an empty list, which is a poor first screen for a
teammate opening the shared (offline) copy. seed_demo_returns() runs a handful
of fixture returns through the same service layer the dashboard uses, so every
seeded return has a real transcript, trace and draft; nothing is faked. Each
one stops at a different point so the list shows the states a preparer meets:
complete, halted, waiting on a person, and mid-conversation.

Seeding runs when the index is empty and SILVERFILE_SEED_DEMO is set (the
Dockerfile sets it; `python run.py seed` runs it by hand). Offline it costs no
tokens; with an API key it would spend a little on each run.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import service
from .config import ROOT
from .security import auth

SEEDER = auth.system("demo-seed")  # the seed acts as the system, through the same checks as a person

# Each entry: which fixture's documents, which intake (None = hold the conversation),
# an optional name/field patch, scripted answers, and how a person resolves each
# escalation kind. An escalation with no entry is left open ("waiting on a person").
DEMO_RETURNS: list[dict[str, Any]] = [
    {
        "fixture": "case1_clean", "intake": "case1_clean",
        "why": "the happy path: four documents, one review round, complete",
    },
    {
        "fixture": "case2_hitl", "intake": "case2_hitl",
        "resolution": {"filing_status": {"action": "continue", "note": "MFJ for the year of death", "facts_patch": {"filing_status": "mfj"}}},
        "why": "spouse died during the year; a person chose MFJ and the draft completed",
    },
    {
        "fixture": "case3_out_of_scope", "intake": "case1_clean",
        "intake_patch": {"taxpayer": {"first_name": "Ruth", "dob": "1951-07-09", "last_name": "Adler"}, "documents_declared": ["SSA-1099"]},
        "resolution": {"out_of_scope": {"action": "halt", "note": "Schedule C: a preparer takes it from here"}},
        "why": "a 1099-NEC turned up; halted and handed to a preparer",
    },
    {
        "fixture": "case4_r_box2b", "intake": "case1_clean",
        "intake_patch": {"taxpayer": {"first_name": "Samuel", "dob": "1947-02-22", "last_name": "Ortega"}, "documents_declared": ["1099-R"]},
        "why": "1099-R box 2b checked; still waiting on a person",
    },
    {
        "fixture": "case1_clean", "intake": "case1_clean",
        "intake_patch": {"taxpayer": {"first_name": "Margaret", "dob": "1956-10-30", "last_name": "Okafor"}, "refund_account_is_own": False, "refund_account_description": "the account my nephew set up for me"},
        "why": "refund account not the taxpayer's own; exploitation check waiting on a person",
    },
    {
        "fixture": "case1_clean", "intake": None, "answers": 9,
        "why": "a conversation in progress: nine answers in, the next question is waiting",
    },
]

MAX_STEPS = 60


def _intake(name: str | None, patch: dict[str, Any] | None) -> dict[str, Any] | None:
    if name is None:
        return None
    intake = json.loads(json.dumps(service.fixture_intake(name)))
    for k, v in (patch or {}).items():
        if k in ("taxpayer", "spouse") and isinstance(v, dict):
            intake[k] = {**(intake.get(k) or {}), **v}
        else:
            intake[k] = v
    return intake


def seed_one(spec: dict[str, Any]) -> str:
    """Create and drive one demo return; returns its id."""
    fixture = spec["fixture"]
    docs = service.fixture_documents(fixture)
    intake = _intake(spec.get("intake"), spec.get("intake_patch"))
    answers = service.fixture_answers(fixture)[: spec.get("answers", 0)] if intake is None else []
    resolution = spec.get("resolution") or {}
    rid = service.create_return([] if intake is None else docs, intake, return_id="demo-" + service.new_return_id()[4:], fixture=fixture, actor=SEEDER)
    service.run(rid, actor=SEEDER)
    for _ in range(MAX_STEPS):
        p = service.pending(rid)
        if not p:
            break
        kind = p.get("type")
        if kind == "escalation":
            res = resolution.get(p["escalation"]["kind"])
            if res is None:
                break
            service.resolve(rid, {"resolved_by": "demo-seed", **res}, actor=SEEDER)
        elif kind in ("question", "upload"):
            if not answers:
                break
            text = answers.pop(0)
            service.answer(rid, text, docs if kind == "upload" else None, actor=SEEDER)
        else:
            break
    return rid


def seed_demo_returns(*, force: bool = False) -> list[str]:
    """Seed the demo set when the index is empty (or always, with force). Returns the new ids."""
    if not force and service.list_returns():
        return []
    return [seed_one(spec) for spec in DEMO_RETURNS]


def seed_enabled() -> bool:
    return os.environ.get("SILVERFILE_SEED_DEMO", "0").strip().lower() in ("1", "true", "on", "yes")


__all__ = ["DEMO_RETURNS", "ROOT", "seed_demo_returns", "seed_enabled", "seed_one"]
