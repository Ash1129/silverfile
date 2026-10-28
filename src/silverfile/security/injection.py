"""Instruction-like text in places that should only hold data.

Prompt injection reaches SilverFile two ways: a taxpayer types it into an
answer, or it is printed on (or hidden in) an uploaded document. Detection here
is a tripwire, not the defence. The defences are structural:
- model output is parsed into a fixed schema and anything outside it is dropped
  (agents/intake.sanitize_parse, security/validate.clean_fields);
- no model has tools, so a model can only return text, never act;
- every amount on the draft comes from a tax_math call with a provenance record;
- the escalation triggers and the review rules are code, which text cannot switch off.
A match here records a flag the reviewer and a person see; it changes no value.
"""
from __future__ import annotations

import re
from typing import Any

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore-instructions", re.compile(r"\b(ignore|disregard|override)\b.{0,30}\b(instructions?|prompts?|rules|guidelines|directions)\b|\b(ignore|disregard)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\b", re.I | re.S)),
    ("role-change", re.compile(r"\b(you are now|act as|pretend (to be|you are)|from now on you)\b", re.I)),
    ("system-prompt", re.compile(r"\b(system prompt|developer message|hidden instructions?|jailbreak)\b", re.I)),
    ("fake-role-tag", re.compile(r"(^|\n|\s)(system|assistant|developer)\s*:|</?\s*(system|instructions?|admin)\s*>", re.I)),
    ("silence-escalation", re.compile(r"\b(do not|don't|never)\s+(flag|escalate|report|review|alert|notify)\b|\bescalation\s+(is\s+)?(resolved|closed|cleared|not needed)\b", re.I)),
    ("force-approval", re.compile(r"\b(mark|set|treat)\b.{0,30}\b(passed|approved|verified|complete|clean)\b|\bapproved by (the )?irs\b|\breview (passed|complete)\b", re.I | re.S)),
    ("set-value", re.compile(r"\b(set|change|report|enter|use)\b.{0,20}\b(wages|withholding|box\s*\d+\w?|line\s*\d+\w?|refund|amount)\b.{0,20}\b(to|as|=)\b", re.I | re.S)),
]


# A taxpayer legitimately says "change the amount to 600" or "mark it complete";
# on a printed tax form those phrases have no business appearing.
DOCUMENT_ONLY = {"set-value", "force-approval"}


def suspicious(text: Any, *, answer: bool = False) -> list[str]:
    """Names of the patterns the text matches; [] for ordinary data.

    answer=True is for a taxpayer's reply, where the document-only patterns are skipped.
    """
    s = str(text or "")
    if len(s) < 8:
        return []
    return [name for name, rx in PATTERNS if not (answer and name in DOCUMENT_ONLY) and rx.search(s)]


def scan_values(values: dict[str, Any]) -> list[str]:
    """One flag per string field that looks like an instruction, e.g. 'employer_name: ignore-instructions'."""
    out: list[str] = []
    for k, v in values.items():
        if isinstance(v, str):
            hits = suspicious(v)
            if hits:
                out.append(f"{k}: {', '.join(hits)}")
    return out
