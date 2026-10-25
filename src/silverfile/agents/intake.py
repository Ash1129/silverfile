"""Intake agent: the only agent that talks to the taxpayer.

One question per graph step. The node asks its question through a LangGraph
`interrupt`, so every answer is a checkpoint: a session abandoned on Tuesday
resumes on Friday at the same question, with a summary of what was covered.

The agent does no arithmetic and interprets no tax rule. It asks for the
filing status the taxpayer expects to use rather than deriving one; "I'm not
sure" is an acceptable answer and is noted for the preparer (or escalated when
the answer matters, such as filing status).

Answers are parsed by a small Claude model into slot updates (any slot, so a
re-answer or a correction is never lost). Without an API key, a scripted parser
handles the same slots.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

from .. import llm
from ..config import settings
from ..provenance import now_iso
from ..state import Escalation, IntakeFacts, ReturnState

SSN_RE = re.compile(r"\b(\d{3})[- ]?(\d{2})[- ]?(\d{4})\b")
ADVICE_RE = re.compile(r"\b(should i|would i pay less|can i deduct|is it better|better to|roth|convert my ira|save on tax|lower my tax|write off|claim .* deduction|file separately to)\b", re.I)
GO_BACK_RE = re.compile(r"\b(go back|change|correct|fix|actually|i meant|earlier i said|wrong)\b", re.I)
UNSURE_RE = re.compile(r"\b(not sure|don't know|dont know|no idea|unsure|can't remember|cannot remember|i forget)\b", re.I)
THIRD_PARTY_RE = re.compile(r"\b(my (mother|father|mom|dad|husband|wife|aunt|uncle|grandmother|grandfather|client|neighbor|neighbour)|answering for|on (his|her|their) behalf|i am (his|her|their) (son|daughter|nephew|niece|caregiver|friend|neighbor))\b", re.I)
DOC_KINDS = {"W-2": ["w-2", "w2", "wage"], "SSA-1099": ["ssa", "social security"], "1099-R": ["1099-r", "1099r", "pension", "annuity", "ira distribution", "retirement"], "1099-INT": ["1099-int", "1099int", "interest"], "1099-DIV": ["1099-div", "1099div", "dividend"], "1099-NEC": ["1099-nec", "nec", "contractor"], "1099-B": ["1099-b", "brokerage", "stock sale"], "1099-MISC": ["1099-misc", "misc"], "K-1": ["k-1", "k1"]}
FILING_WORDS = {"single": ["single"], "mfj": ["jointly", "joint", "married filing jointly", "mfj", "together"], "mfs": ["separately", "mfs", "separate"], "hoh": ["head of household", "hoh"], "qss": ["surviving spouse", "qualifying widow", "widower", "qss"]}
MAX_TURNS = 80  # a safety stop on a conversation that never completes

GLOSSES = {
    "standard deduction": "the standard deduction is the fixed amount of income that is not taxed",
    "filing status": "your filing status is the category the IRS uses for your household, such as single or married filing jointly",
    "dependents": "a dependent is a child or relative you support and can list on your return",
    "estimated tax payments": "estimated tax payments are amounts some people send the IRS during the year, often every three months",
    "adjustments to income": "adjustments to income are a short list of items, such as a deductible IRA contribution, that reduce your income before tax",
    "withholding": "withholding is tax your employer or payer already sent to the IRS for you",
}


# ---------------------------------------------------------------- questions
def _q(slot: str, facts: IntakeFacts) -> str:
    tp = facts.get("taxpayer", {}) or {}
    first = tp.get("first_name") or "there"
    sp = facts.get("spouse") or {}
    questions = {
        "answering_party": "Hello, I'm SilverFile. I help put together a draft of your tax return for a Kushim preparer to check; I never file anything and I can't give tax advice. First, am I speaking with the person whose tax return this is?",
        "name": "Thank you. What is your full name, as it appears on your Social Security card?",
        "ssn": f"Thanks, {first}. What Social Security number should go on the return? (This is a demonstration, so only test numbers that start with 900 to 999 are accepted. Please don't type a real one.)",
        "dob": "What is your date of birth?",
        "blind": "Are you legally blind? The IRS asks because it changes the standard deduction, which is the fixed amount of income that is not taxed. A simple yes or no is fine.",
        "marital": "At the end of 2025, were you single, married, widowed, or divorced?",
        "spouse_died_in_year": "I'm sorry for your loss. Did your spouse pass away during 2025?",
        "filing_status": "Which filing status do you expect to use? The choices are single, married filing jointly, married filing separately, head of household, or qualifying surviving spouse. If you're not sure, just say so and I'll note it for the preparer.",
        "spouse_name": "What is your spouse's full name?",
        "spouse_dob": f"What is {sp.get('first_name') or 'your spouse'}'s date of birth?",
        "spouse_blind": f"Is {sp.get('first_name') or 'your spouse'} legally blind?",
        "dependents": "Will you list any dependents on this return? A dependent is a child or relative you support. Say none, or tell me the first one's name and how they are related to you.",
        "dependent_ssn": f"What is the Social Security number for {(facts.get('dependents') or [{}])[-1].get('name', 'that dependent')}? (Test numbers starting 900 to 999 only.)",
        "more_dependents": "Is there another dependent to list? Say no, or give me their name and relationship.",
        "can_be_claimed": "Can someone else claim you as a dependent on their own tax return? Most retirees answer no.",
        "documents_declared": "Which tax forms did you receive for 2025? For example a W-2 from an employer, an SSA-1099 for Social Security, a 1099-R for a pension or IRA, a 1099-INT for bank interest, or a 1099-DIV for dividends. Just list what you have.",
        "other_income": "Did you have any other income in 2025 that isn't on one of those forms? For example from a business or freelance work, renting out property, selling investments or cryptocurrency, or from outside the United States. Say no, or describe it briefly.",
        "estimated_payments": "Did you send the IRS any estimated tax payments during 2025? Those are payments some people make every three months. If so, roughly how much in total? Otherwise say none.",
        "schedule1_adjustments": "Do you have any adjustments to income to report? Those are a short list of items, such as a deductible IRA contribution or student loan interest, that reduce your income before tax. Give a total, or say none.",
        "schedule1_other_income": "Did a preparer give you an amount for 'additional income' on Schedule 1? If you don't know what that is, say none.",
        "schedule2_line3": "Did a preparer give you an amount for additional taxes on Schedule 2? If not, say none.",
        "schedule3_line8": "Did a preparer give you an amount for credits on Schedule 3? If not, say none.",
        "refund_account": "If you're due a refund, where should it go? Describe the account in your own words, for example 'my checking account at First Bank'.",
        "refund_account_is_own": "Is that account in your own name?",
        "upload": "Last step: please upload your tax forms now. A photo of each W-2 is fine. When you're done, say done.",
        "confirm": "Here's what I have. " + summarize_facts(facts) + " Is that all correct? Say yes, or tell me what to change.",
    }
    return questions[slot]


def summarize_facts(facts: IntakeFacts) -> str:
    tp = facts.get("taxpayer", {}) or {}
    parts = []
    if tp.get("first_name"):
        parts.append(f"Your name is {tp.get('first_name', '')} {tp.get('last_name', '')}".strip() + ".")
    if tp.get("dob"):
        parts.append(f"You were born on {tp['dob']}.")
    if facts.get("filing_status"):
        names = {"single": "single", "mfj": "married filing jointly", "mfs": "married filing separately", "hoh": "head of household", "qss": "qualifying surviving spouse"}
        parts.append(f"Filing status: {names.get(facts['filing_status'], facts['filing_status'])}.")
    if facts.get("spouse_died_in_year"):
        parts.append("Your spouse passed away during 2025, which a preparer will look at.")
    deps = facts.get("dependents") or []
    parts.append(f"Dependents: {', '.join(d.get('name', '?') for d in deps) if deps else 'none'}.")
    if facts.get("documents_declared"):
        parts.append(f"Forms: {', '.join(facts['documents_declared'])}.")
    if facts.get("estimated_payments") is not None:
        parts.append(f"Estimated payments: ${facts['estimated_payments']:,.2f}.")
    if facts.get("noted_questions"):
        parts.append(f"Questions noted for the preparer: {len(facts['noted_questions'])}.")
    return " ".join(parts) or "Nothing recorded yet."


def next_slot(facts: IntakeFacts) -> str | None:
    tp = facts.get("taxpayer", {}) or {}
    if not facts.get("answering_party"):
        return "answering_party"
    if not tp.get("last_name"):
        return "name"
    if not tp.get("ssn"):
        return "ssn"
    if not tp.get("dob"):
        return "dob"
    if tp.get("blind") is None:
        return "blind"
    if facts.get("married") is None:
        return "marital"
    if facts.get("_widowed") and facts.get("spouse_died_in_year") is None:
        return "spouse_died_in_year"
    if facts.get("filing_status") is None and not facts.get("_filing_status_unsure"):
        return "filing_status"
    if facts.get("filing_status") in ("mfj", "mfs") or facts.get("spouse_died_in_year"):
        sp = facts.get("spouse") or {}
        if not sp.get("last_name"):
            return "spouse_name"
        if facts.get("filing_status") == "mfj" or facts.get("spouse_died_in_year"):
            if not sp.get("dob"):
                return "spouse_dob"
            if sp.get("blind") is None:
                return "spouse_blind"
    if facts.get("dependents") is None:
        return "dependents"
    deps = facts.get("dependents") or []
    if deps and not deps[-1].get("ssn"):
        return "dependent_ssn"
    if deps and not facts.get("_dependents_done"):
        return "more_dependents"
    if facts.get("can_be_claimed_as_dependent") is None:
        return "can_be_claimed"
    if facts.get("documents_declared") is None:
        return "documents_declared"
    if facts.get("other_income_sources") is None:
        return "other_income"
    for slot in ("estimated_payments", "schedule1_adjustments", "schedule1_other_income", "schedule2_line3", "schedule3_line8"):
        if slot not in facts:
            return slot
    if facts.get("refund_account_description") is None:
        return "refund_account"
    if facts.get("refund_account_is_own") is None:
        return "refund_account_is_own"
    if not facts.get("_uploaded"):
        return "upload"
    if not facts.get("_confirmed"):
        return "confirm"
    return None


# ---------------------------------------------------------------- parsing
def valid_test_ssn(s: str) -> tuple[bool, str | None]:
    m = SSN_RE.search(s or "")
    if not m:
        return False, None
    area = int(m.group(1))
    ssn = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return 900 <= area <= 999, ssn


def parse_date(s: str) -> str | None:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%d %B %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s) or re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            g = m.groups()
            return (datetime(int(g[0]), int(g[1]), int(g[2])) if len(g[0]) == 4 else datetime(int(g[2]), int(g[0]), int(g[1]))).date().isoformat()
        except ValueError:
            return None
    return None


def parse_money(s: str) -> float | None:
    if re.search(r"\b(none|no|nothing|zero|didn't|did not|nope)\b", s, re.I):
        return 0.0
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", s)
    return float(m.group(1).replace(",", "")) if m else None


def parse_yes_no(s: str) -> bool | None:
    if re.search(r"\b(yes|yeah|yep|correct|i am|that's right|right|true|sure)\b", s, re.I):
        return True
    if re.search(r"\b(no|nope|not|never|false|none)\b", s, re.I):
        return False
    return None


def parse_doc_kinds(s: str) -> list[str]:
    low = s.lower()
    return [k for k, needles in DOC_KINDS.items() if any(n in low for n in needles)]


def scripted_parse(slot: str, text: str, facts: IntakeFacts) -> dict[str, Any]:
    """Offline parser: returns {"updates": {...}, "intent": ...}."""
    t = text.strip()
    up: dict[str, Any] = {}
    intent = "answer"
    if slot == "answering_party":
        if THIRD_PARTY_RE.search(t) or re.search(r"\b(daughter|son|nephew|niece|caregiver|power of attorney|poa)\b", t, re.I):
            auth = bool(re.search(r"\b(power of attorney|poa|authori[sz]ed|permission|form 2848|guardian|conservator)\b", t, re.I))
            up["answering_party"] = "third_party_with_authority" if auth else "third_party_no_authority"
            up["third_party_authority"] = t
        elif parse_yes_no(t) is not False:
            up["answering_party"] = "self"
        else:
            up["answering_party"] = "third_party_no_authority"
            up["third_party_authority"] = t
    elif slot in ("name", "spouse_name"):
        words = [w for w in re.sub(r"[^A-Za-z' -]", " ", t).split() if w.lower() not in ("my", "name", "is", "it's", "its", "i'm", "im", "the", "spouse", "wife", "husband", "her", "his", "named", "called")]
        if words:
            up["first_name"], up["last_name"] = words[0].title(), words[-1].title() if len(words) > 1 else ""
    elif slot in ("ssn", "dependent_ssn"):
        ok, ssn = valid_test_ssn(t)
        up["_ssn_ok"], up["_ssn"] = ok, ssn
    elif slot in ("dob", "spouse_dob"):
        up["dob"] = parse_date(t)
    elif slot in ("blind", "spouse_blind", "can_be_claimed", "refund_account_is_own", "spouse_died_in_year"):
        up["value"] = parse_yes_no(t)
    elif slot == "marital":
        low = t.lower()
        up["marital"] = "widowed" if "widow" in low else "married" if "married" in low else "divorced" if "divorc" in low else "single" if "single" in low else None
    elif slot == "filing_status":
        low = t.lower()
        up["filing_status"] = next((k for k, ws in FILING_WORDS.items() if any(w in low for w in ws)), None)
    elif slot in ("dependents", "more_dependents"):
        if parse_yes_no(t) is False or re.search(r"\b(none|no one|nobody)\b", t, re.I):
            up["none"] = True
        else:
            m = re.match(r"\s*([A-Za-z' .-]+?)(?:,| is| -|\s+my\s+|\s*\()\s*(.*)$", t)
            name = (m.group(1) if m else t).strip().title()
            rel = (m.group(2) if m else "").strip(" )").lower() or "dependent"
            up["dependent"] = {"name": name, "relationship": rel.replace("my ", "")}
    elif slot == "documents_declared":
        up["kinds"] = parse_doc_kinds(t)
    elif slot == "other_income":
        up["other"] = [] if parse_yes_no(t) is False and len(t) < 40 else [t]
    elif slot in ("estimated_payments", "schedule1_adjustments", "schedule1_other_income", "schedule2_line3", "schedule3_line8"):
        up["amount"] = parse_money(t)
    elif slot == "refund_account":
        up["description"] = t
        if UNSURE_RE.search(t) or re.search(r"\b(set it up|his account|her account|their account|my (son|daughter|nephew|niece)'s)\b", t, re.I):
            up["cannot_describe"] = True
    elif slot == "upload":
        up["done"] = True
    elif slot == "confirm":
        yn = parse_yes_no(t)
        up["confirmed"] = yn is True and not GO_BACK_RE.search(t)
        if not up["confirmed"]:
            up["go_back_to"] = guess_slot(t)
    return {"updates": up, "intent": intent}


SLOT_HINTS = {"dob": ["birth", "born", "dob"], "name": ["name"], "filing_status": ["filing status", "status", "jointly", "single"], "ssn": ["social", "ssn"], "dependents": ["dependent", "child"], "documents_declared": ["form", "w-2", "1099"], "estimated_payments": ["estimated"], "marital": ["married", "widow", "divorc"], "blind": ["blind"], "refund_account": ["refund", "account", "bank"], "schedule1_adjustments": ["adjustment"]}


def guess_slot(text: str) -> str | None:
    low = text.lower()
    for slot, words in SLOT_HINTS.items():
        if any(w in low for w in words):
            return slot
    return None


LLM_SYSTEM = """You parse one answer from a taxpayer in a plain-language tax intake. You never give tax advice and never decide tax rules.
Given the current question slot and the answer, return JSON:
{"intent": "answer|advice_question|go_back|unsure|third_party|other",
 "updates": {... slot-specific fields ...},
 "go_back_to": null or one of the known slots,
 "note": short note for the preparer or null}
Slot-specific update fields:
- answering_party: {"answering_party": "self|third_party_with_authority|third_party_no_authority", "third_party_authority": text or null}
- name/spouse_name: {"first_name": ..., "last_name": ...}
- ssn/dependent_ssn: {"ssn": "ddd-dd-dddd" or null}
- dob/spouse_dob: {"dob": "YYYY-MM-DD" or null}
- blind/spouse_blind/can_be_claimed/refund_account_is_own/spouse_died_in_year: {"value": true|false|null}
- marital: {"marital": "single|married|widowed|divorced|null"}
- filing_status: {"filing_status": "single|mfj|mfs|hoh|qss|null"}
- dependents/more_dependents: {"none": true} or {"dependent": {"name": ..., "relationship": ...}}
- documents_declared: {"kinds": ["W-2","SSA-1099","1099-R","1099-INT","1099-DIV","1099-NEC","1099-B","1099-MISC","K-1", ...]}
- other_income: {"other": [free-text descriptions, or empty list]}
- estimated_payments/schedule1_adjustments/schedule1_other_income/schedule2_line3/schedule3_line8: {"amount": number or null}
- refund_account: {"description": text, "cannot_describe": true|false}
- upload: {"done": true}
- confirm: {"confirmed": true|false}
If the answer also corrects an earlier slot (e.g. "actually I was born in 1955"), include that slot's fields under "also": {"slot": {...}}.
Return JSON only."""


def llm_parse(slot: str, text: str, facts: IntakeFacts) -> tuple[dict[str, Any], int, int]:
    res = llm.complete(LLM_SYSTEM, [{"role": "user", "content": json.dumps({"slot": slot, "answer": text, "known": {k: v for k, v in facts.items() if not k.startswith("_") and k != "taxpayer"} | {"taxpayer_first_name": (facts.get("taxpayer") or {}).get("first_name")}})}], model=settings().intake_model, max_tokens=400)
    data = res.json()
    if not isinstance(data, dict):
        data = scripted_parse(slot, text, facts)
    data.setdefault("updates", {})
    data.setdefault("intent", "answer")
    if slot in ("ssn", "dependent_ssn"):
        ok, ssn = valid_test_ssn(text)
        data["updates"] = {"_ssn_ok": ok, "_ssn": ssn}
    return data, res.tokens_in, res.tokens_out


# ---------------------------------------------------------------- applying
def apply(slot: str, parsed: dict[str, Any], facts: IntakeFacts, reply: list[str]) -> IntakeFacts:
    f: IntakeFacts = json.loads(json.dumps(facts))  # deep copy; TypedDict is a plain dict
    up = parsed.get("updates") or {}
    tp = f.setdefault("taxpayer", {})
    sp = f.get("spouse") or {}
    if slot == "answering_party":
        f["answering_party"] = up.get("answering_party") or "unknown"
        f["third_party_authority"] = up.get("third_party_authority")
        if f["answering_party"] == "third_party_no_authority":
            reply.append("Thank you for telling me. Because you're answering for someone else, a Kushim employee will need to confirm you're authorised before we go further. I'll pause here and let them know.")
    elif slot == "name":
        if up.get("last_name") or up.get("first_name"):
            tp["first_name"], tp["last_name"] = up.get("first_name", ""), up.get("last_name", "")
        else:
            reply.append("I didn't catch a name there.")
    elif slot == "spouse_name":
        if up.get("last_name") or up.get("first_name"):
            f["spouse"] = {**sp, "first_name": up.get("first_name", ""), "last_name": up.get("last_name", "")}
    elif slot in ("ssn", "dependent_ssn"):
        if up.get("_ssn") is None:
            reply.append("I didn't see a number in the format 9xx-xx-xxxx.")
        elif not up.get("_ssn_ok"):
            reply.append("That number doesn't start with 900 to 999, so it may be a real Social Security number. I have not kept it. Please use a test number starting with 900 to 999.")
            f.setdefault("volunteered_ssn_events", []).append(f"{now_iso()}: a non-test SSN was entered at slot {slot}; rejected and not stored")
        elif slot == "ssn":
            tp["ssn"] = up["_ssn"]
        else:
            f["dependents"][-1]["ssn"] = up["_ssn"]
    elif slot == "dob":
        if up.get("dob"):
            tp["dob"] = up["dob"]
        else:
            reply.append("I couldn't read that as a date. Month, day and year is easiest, like 3/14/1952.")
    elif slot == "spouse_dob":
        if up.get("dob"):
            f["spouse"] = {**sp, "dob": up["dob"]}
        else:
            reply.append("I couldn't read that as a date. Month, day and year is easiest, like 3/14/1952.")
    elif slot == "blind":
        if up.get("value") is not None:
            tp["blind"] = bool(up["value"])
    elif slot == "spouse_blind":
        if up.get("value") is not None:
            f["spouse"] = {**sp, "blind": bool(up["value"])}
    elif slot == "marital":
        m = up.get("marital")
        if m:
            f["married"] = m == "married"
            f["_widowed"] = m == "widowed"
            if m != "widowed":
                f["spouse_died_in_year"] = False
    elif slot == "spouse_died_in_year":
        if up.get("value") is not None:
            f["spouse_died_in_year"] = bool(up["value"])
            if f["spouse_died_in_year"]:
                f["married"] = True
                f["_filing_status_unsure"] = True
                reply.append("Thank you. When a spouse passes away during the year there is more than one filing status that could apply, and that's a decision for a Kushim preparer, not for me. I'll flag it for them and we'll carry on with the rest.")
    elif slot == "filing_status":
        if up.get("filing_status"):
            f["filing_status"] = up["filing_status"]
        elif parsed.get("intent") == "unsure" or UNSURE_RE.search(parsed.get("_text", "")):
            f["_filing_status_unsure"] = True
            f.setdefault("noted_questions", []).append("Taxpayer is not sure of their filing status.")
            reply.append("That's fine. I've noted it for the preparer, who will settle the filing status with you.")
    elif slot in ("dependents", "more_dependents"):
        if up.get("none"):
            f.setdefault("dependents", [])
            f["_dependents_done"] = True
        elif up.get("dependent"):
            f.setdefault("dependents", []).append({"name": up["dependent"].get("name", ""), "relationship": up["dependent"].get("relationship", ""), "ssn": ""})
            f["_dependents_done"] = False
    elif slot == "can_be_claimed":
        if up.get("value") is not None:
            f["can_be_claimed_as_dependent"] = bool(up["value"])
        elif parsed.get("intent") == "unsure":
            f["can_be_claimed_as_dependent"] = False
            f.setdefault("noted_questions", []).append("Taxpayer unsure whether someone can claim them as a dependent; assumed no.")
    elif slot == "documents_declared":
        f["documents_declared"] = up.get("kinds") or []
        if not f["documents_declared"]:
            reply.append("I didn't recognise a form name. That's all right; you can upload whatever you have in a moment.")
    elif slot == "other_income":
        f["other_income_sources"] = up.get("other") or []
    elif slot in ("estimated_payments", "schedule1_adjustments", "schedule1_other_income", "schedule2_line3", "schedule3_line8"):
        amt = up.get("amount")
        if amt is None and parsed.get("intent") == "unsure":
            f[slot] = None  # type: ignore[literal-required]
            f.setdefault("noted_questions", []).append(f"Taxpayer unsure about {slot.replace('_', ' ')}; left blank for the preparer.")
        elif amt is None:
            reply.append("I need a number or 'none' for that one.")
        else:
            f[slot] = float(amt)  # type: ignore[literal-required]
    elif slot == "refund_account":
        f["refund_account_description"] = up.get("description") or parsed.get("_text")
        if up.get("cannot_describe"):
            f["refund_account_is_own"] = False
    elif slot == "refund_account_is_own":
        if up.get("value") is not None:
            f["refund_account_is_own"] = bool(up["value"])
    elif slot == "upload":
        f["_uploaded"] = True
    elif slot == "confirm":
        if up.get("confirmed"):
            f["_confirmed"] = True
        else:
            target = parsed.get("go_back_to") or up.get("go_back_to")
            _clear_slot(f, target)
            reply.append("No problem, let's fix that." if target else "Tell me which item to change, for example 'my date of birth'.")
    # corrections to other slots volunteered in this answer
    for other, fields in (parsed.get("also") or {}).items():
        if other != slot and isinstance(fields, dict):
            f = apply(other, {"updates": fields, "intent": "answer"}, f, reply)
    return f


def _clear_slot(f: IntakeFacts, slot: str | None) -> None:
    tp = f.setdefault("taxpayer", {})
    if slot in ("dob", "name", "ssn", "blind"):
        for k in {"dob": ["dob"], "name": ["first_name", "last_name"], "ssn": ["ssn"], "blind": ["blind"]}[slot]:
            tp.pop(k, None)
    elif slot == "filing_status":
        f["filing_status"] = None
        f.pop("_filing_status_unsure", None)
    elif slot == "marital":
        f["married"] = None
        f["filing_status"] = None
    elif slot == "dependents":
        f["dependents"] = None  # type: ignore[typeddict-item]
        f.pop("_dependents_done", None)
    elif slot in ("documents_declared", "estimated_payments", "schedule1_adjustments", "refund_account"):
        f.pop({"refund_account": "refund_account_description"}.get(slot, slot), None)  # type: ignore[misc]
        if slot == "refund_account":
            f.pop("refund_account_is_own", None)
    f.pop("_confirmed", None)


# ---------------------------------------------------------------- the node
def run_intake(state: ReturnState) -> dict[str, Any]:
    facts: IntakeFacts = json.loads(json.dumps(state.get("intake") or {}))
    turns = int(facts.get("_turns", 0))
    slot = next_slot(facts)
    if slot is None or turns >= MAX_TURNS:
        return {"intake": facts, "intake_complete": True, "intake_cursor": None, "handoffs": ["intake"], "events": ["intake: complete" if slot is None else "intake: turn limit reached; handing to a person"], **({"escalations": [Escalation(id="esc-intake-turns", kind="conflict", reason="The intake conversation did not complete within the turn limit.", raised_by="intake", raised_at=now_iso(), details={}, status="open", resolution=None, resolved_by=None, resolved_at=None)]} if slot is not None else {})}

    # resume summary after a gap
    last = facts.get("_last_activity_at")
    gap_min = 0.0
    if last:
        gap_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
    prefix = ""
    if last and gap_min > 30 and slot not in ("answering_party",):
        prefix = "Welcome back. Here's where we were: " + summarize_facts(facts) + " Let's continue. "
    question = prefix + _q(slot, facts)

    # ---- ask (checkpoint) and receive -------------------------------------
    answer = interrupt({"type": "upload" if slot == "upload" else "question", "slot": slot, "text": question, "summary": summarize_facts(facts), "return_id": state.get("return_id")})
    text = answer if isinstance(answer, str) else json.dumps(answer) if not isinstance(answer, dict) else str(answer.get("text", ""))
    new_docs = answer.get("documents") if isinstance(answer, dict) else None
    reply: list[str] = []
    tokens_in = tokens_out = 0
    escalations: list[Escalation] = []
    events: list[str] = []

    # ---- deliberate handling of things that are not answers --------------
    if slot not in ("ssn", "dependent_ssn"):
        ok, ssn = valid_test_ssn(text)
        if ssn:
            facts.setdefault("volunteered_ssn_events", []).append(f"{now_iso()}: an SSN was volunteered at slot {slot}; {'test series' if ok else 'NOT a test number'}; not stored")
            reply.append("I noticed a Social Security number in your answer. I didn't need it here, so I haven't kept it.")
            text = SSN_RE.sub("[number removed]", text)
    if ADVICE_RE.search(text) and slot not in ("upload",):
        facts.setdefault("noted_questions", []).append(text.strip())
        reply.append("That's a question about what would be best for you, which is tax advice, and I'm not able to give advice; I only help gather and copy the figures. I've noted your question for the Kushim preparer, who can talk it through with you.")
        escalations.append(Escalation(id=f"esc-advice-{turns}", kind="advice", reason=f"Taxpayer asked for tax advice: {text.strip()[:200]!r}", raised_by="intake", raised_at=now_iso(), details={"slot": slot}, status="open", resolution=None, resolved_by=None, resolved_at=None))
        events.append("intake: advice question noted and escalated")
        # keep the answers we already have; ask the same question again next turn
        return _finish(state, facts, slot, question, text, reply, turns, escalations, events, tokens_in, tokens_out, repeat=True)
    if THIRD_PARTY_RE.search(text) and facts.get("answering_party") == "self" and slot != "answering_party":
        facts["answering_party"] = "third_party_no_authority"
        facts["third_party_authority"] = text
        reply.append("It sounds like someone else is answering now. A Kushim employee will need to confirm that's authorised, so I'll pause here.")
        events.append("intake: third party detected mid-conversation")
    if GO_BACK_RE.search(text) and slot not in ("confirm", "answering_party", "name") and not re.search(r"\d", text) and guess_slot(text) and guess_slot(text) != slot:
        target = guess_slot(text)
        _clear_slot(facts, target)
        reply.append("Of course, let's go back to that.")
        return _finish(state, facts, slot, question, text, reply, turns, escalations, events + [f"intake: user went back to {target}"], tokens_in, tokens_out, repeat=False)

    # ---- parse and apply ----------------------------------------------------
    if slot == "upload":
        parsed = {"updates": {"done": True}, "intent": "answer"}
    elif llm.available():
        try:
            parsed, tokens_in, tokens_out = llm_parse(slot, text, facts)
        except Exception as e:  # never lose the turn to a model error
            parsed = scripted_parse(slot, text, facts)
            events.append(f"intake: model parse failed ({e}); scripted parser used")
    else:
        parsed = scripted_parse(slot, text, facts)
    parsed["_text"] = text
    if UNSURE_RE.search(text) and parsed.get("intent") == "answer":
        parsed["intent"] = "unsure"
    before = json.dumps(facts, sort_keys=True)
    facts = apply(slot, parsed, facts, reply)
    if parsed.get("intent") == "unsure" and json.dumps(facts, sort_keys=True) == before and slot in ("filing_status", "dob", "ssn", "name"):
        if slot == "filing_status":
            facts["_filing_status_unsure"] = True
        reply.append("That's all right. I'm not sure either, so I'll flag it for a volunteer preparer rather than guess." if slot != "filing_status" else "")
        facts.setdefault("noted_questions", []).append(f"Taxpayer unsure at {slot}.")
        if slot != "filing_status":
            escalations.append(Escalation(id=f"esc-unsure-{slot}", kind="conflict", reason=f"Taxpayer could not answer '{slot}' and the system cannot proceed without it.", raised_by="intake", raised_at=now_iso(), details={}, status="open", resolution=None, resolved_by=None, resolved_at=None))
    if facts.get("_filing_status_unsure") and facts.get("filing_status") is None and not any(e.get("kind") == "filing_status" for e in [*state.get("escalations", []), *escalations]):
        escalations.append(Escalation(id="esc-filing-status", kind="filing_status", reason="Filing status is ambiguous (spouse died during the year, or the taxpayer is unsure).", raised_by="intake", raised_at=now_iso(), details={"spouse_died_in_year": facts.get("spouse_died_in_year")}, status="open", resolution=None, resolved_by=None, resolved_at=None))
    if facts.get("answering_party") == "third_party_no_authority" and not any(e.get("kind") == "exploitation" for e in [*state.get("escalations", []), *escalations]):
        escalations.append(Escalation(id="esc-third-party", kind="exploitation", reason="A third party is answering on the taxpayer's behalf without a stated authority.", raised_by="intake", raised_at=now_iso(), details={"statement": facts.get("third_party_authority")}, status="open", resolution=None, resolved_by=None, resolved_at=None))
    if facts.get("refund_account_is_own") is False and not any(e.get("kind") == "exploitation" and "refund" in e.get("reason", "") for e in [*state.get("escalations", []), *escalations]):
        escalations.append(Escalation(id="esc-refund-account", kind="exploitation", reason=f"The refund would go to an account the taxpayer cannot describe as their own: {facts.get('refund_account_description')!r}.", raised_by="intake", raised_at=now_iso(), details={}, status="open", resolution=None, resolved_by=None, resolved_at=None))
    docs_update = {}
    if slot == "upload" and new_docs:
        docs_update = {"documents_in": [*state.get("documents_in", []), *new_docs]}
    return {**_finish(state, facts, slot, question, text, reply, turns, escalations, events, tokens_in, tokens_out, repeat=False), **docs_update}


def _finish(state: ReturnState, facts: IntakeFacts, slot: str, question: str, text: str, reply: list[str], turns: int, escalations: list[Escalation], events: list[str], tin: int, tout: int, *, repeat: bool) -> dict[str, Any]:
    facts["_turns"] = turns + 1
    facts["_last_activity_at"] = now_iso()
    # the transcript never needs a Social Security number: an accepted test SSN lives in the facts,
    # and a rejected one must not survive in the checkpoint either
    msgs = [AIMessage(content=question), HumanMessage(content=SSN_RE.sub("[number removed]", text))]
    if reply:
        msgs.append(AIMessage(content=" ".join(r for r in reply if r)))
    done = next_slot(facts) is None
    return {
        "intake": facts,
        "intake_complete": done,
        "intake_cursor": slot if repeat else next_slot(facts),
        "messages": msgs,
        "escalations": escalations,
        "handoffs": ["intake"],
        "token_log": [{"agent": "intake", "model": settings().intake_model, "purpose": f"parse {slot}", "tokens_in": tin, "tokens_out": tout, "at": now_iso()}] if tin else [],
        "events": [*events, f"intake: asked {slot}" + (" (complete)" if done else "")],
    }
