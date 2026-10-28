"""Eligibility & Rules agent.

Decides, with a citation for every determination: 1040-SR eligibility, filing
status validity, the line 12d box count, whether the Standard Deduction Chart
applies, which schedules apply, and whether the return is in scope. It raises
escalations for the REF-ES-1 conditions it can see (out-of-scope items,
user/document conflicts, box 2b, filing-status ambiguity, chart not applying).

It never computes a dollar amount; the box count and the eligibility test are
delegated to tax_math so the arithmetic lives in one place.
"""
from __future__ import annotations

from typing import Any

from ..provenance import now_iso
from ..state import Escalation, EligibilityResult, ReturnState
from ..security.capabilities import tools_for

tm = tools_for("eligibility")  # only is_1040sr_eligible, count_12d_boxes and FILING_STATUSES

OUT_OF_SCOPE_KINDS = {
    "1099-NEC": "self-employment / nonemployee compensation (Schedule C)",
    "1099-MISC": "miscellaneous income outside the profile",
    "1099-B": "capital gains requiring Schedule D",
    "1099-K": "business or platform income",
    "1099-S": "real estate sale (Schedule D)",
    "SCHEDULE-A": "itemised deductions (Schedule A)",
    "SCHEDULE-C": "self-employment income (Schedule C)",
    "SCHEDULE-D": "capital gains (Schedule D)",
    "SCHEDULE-E": "rental income (Schedule E)",
    "K-1": "partnership / S-corp income",
    "1042-S": "foreign income",
    "FBAR": "foreign accounts",
}
IN_SCOPE_KINDS = {"W-2", "SSA-1099", "1099-R", "1099-INT", "1099-DIV"}
OUT_OF_SCOPE_PHRASES = [
    ("self-employ", "self-employment income (Schedule C)"), ("freelanc", "self-employment income (Schedule C)"), ("side business", "business income (Schedule C)"),
    ("rental", "rental income (Schedule E)"), ("rent out", "rental income (Schedule E)"), ("airbnb", "rental income (Schedule E)"),
    ("sold stock", "capital gains (Schedule D)"), ("capital gain", "capital gains (Schedule D)"), ("brokerage sale", "capital gains (Schedule D)"),
    ("crypto", "digital assets"), ("bitcoin", "digital assets"), ("digital asset", "digital assets"),
    ("foreign", "foreign income or accounts"), ("overseas account", "foreign accounts"),
    ("itemiz", "itemised deductions (Schedule A)"), ("itemis", "itemised deductions (Schedule A)"),
    ("amended", "amended return"), ("prior year", "prior-year return"), ("last year's return", "prior-year return"), ("state return", "state return"),
]


def _esc(kind: str, reason: str, details: dict[str, Any] | None = None) -> Escalation:
    return Escalation(id=f"esc-{kind}-{abs(hash(reason)) % 10**6}", kind=kind, reason=reason, raised_by="eligibility", raised_at=now_iso(), details=details or {}, status="open", resolution=None, resolved_by=None, resolved_at=None)


def run_eligibility(state: ReturnState) -> dict[str, Any]:
    facts = state.get("intake", {}) or {}
    docs = state.get("extracted", {}) or {}
    tp = facts.get("taxpayer", {}) or {}
    sp = facts.get("spouse") or {}
    fs = facts.get("filing_status")
    cites: list[str] = []
    dets: list[dict[str, Any]] = []
    escalate: list[dict[str, Any]] = []
    out_of_scope: list[str] = []
    conflicts: list[str] = []

    def det(claim: str, result: Any, cite: str) -> None:
        dets.append({"claim": claim, "result": result, "citation": cite})
        if cite not in cites:
            cites.append(cite)

    # 1. Filing status validity / ambiguity (REF-FS-1, REF-ES-1 #5)
    fs_valid = fs in tm.FILING_STATUSES
    if facts.get("spouse_died_in_year"):
        det("Spouse died during the tax year: MFJ vs QSS vs Single is a human decision", "escalate", "REF-FS-1")
        escalate.append({"kind": "filing_status", "reason": "A spouse died during the tax year. Filing status (married filing jointly, qualifying surviving spouse or single) must be decided by a Kushim employee.", "citation": "REF-ES-1"})
    if facts.get("married") is False and fs in ("mfj", "mfs"):
        conflicts.append("Taxpayer said they are not married but a married filing status was chosen.")
    if facts.get("married") is True and fs in ("single",):
        conflicts.append("Taxpayer said they are married but filing status is single.")
    det(f"Filing status {fs!r} is one of the five in-scope statuses", fs_valid, "REF-FS-1")

    # 2. 1040-SR eligibility (REF-EL-1) via tax_math (no date arithmetic here)
    eligible = False
    if tp.get("dob"):
        el = tm.is_1040sr_eligible(tp["dob"], sp.get("dob") if sp else None, fs if fs_valid else "single")
        eligible = bool(el["eligible"])
        det("Taxpayer (or a spouse on a joint return) was born before 2 January 1961", eligible, "REF-EL-1")
        if not eligible:
            escalate.append({"kind": "eligibility", "reason": "Neither the taxpayer nor a spouse on a joint return was born before 2 January 1961, so Form 1040-SR is not the right form.", "citation": "REF-EL-1"})

    # 3. Line 12d boxes (REF-SD-3) via tax_math
    boxes, count = {}, 0
    if tp.get("dob") and fs_valid:
        bx = tm.count_12d_boxes(tp["dob"], bool(tp.get("blind")), sp.get("dob") if sp else None, bool(sp.get("blind")) if sp else False, fs)
        boxes, count = bx["boxes"], bx["count"]
        det("Boxes checked on line 12d", count, "REF-SD-3")

    # 4. Chart applies? (REF-SD-4)
    chart_applies = True
    for flag, why in (("can_be_claimed_as_dependent", "the taxpayer can be claimed as a dependent"), ("mfs_spouse_itemizes", "a separately filing spouse itemises"), ("dual_status_alien", "the taxpayer was a dual-status alien")):
        if facts.get(flag):
            chart_applies = False
            escalate.append({"kind": "chart_not_applicable", "reason": f"The Standard Deduction Chart does not apply because {why}.", "citation": "REF-SD-4"})
    det("Standard Deduction Chart applies", chart_applies, "REF-SD-4")

    # 5. Scope: documents and statements (REF-SC-1, REF-SC-2)
    for d in docs.values():
        kind = (d.get("kind") or "unknown").upper()
        if kind in OUT_OF_SCOPE_KINDS:
            out_of_scope.append(f"{d.get('id')}: {kind} ({OUT_OF_SCOPE_KINDS[kind]})")
        elif kind not in IN_SCOPE_KINDS:
            out_of_scope.append(f"{d.get('id')}: unrecognised document kind {kind!r}")
        if kind == "1099-R" and (d.get("fields", {}).get("taxable_not_determined") in (True, "true", "True", "X", "x", 1)):
            escalate.append({"kind": "r_box2b", "reason": f"1099-R {d.get('id')} has box 2b 'taxable amount not determined' checked; the Simplified Method / General Rule is out of scope.", "citation": "REF-RT-1"})
    for kind in facts.get("documents_declared", []) or []:
        k = kind.upper()
        if k in OUT_OF_SCOPE_KINDS:
            out_of_scope.append(f"declared document {k} ({OUT_OF_SCOPE_KINDS[k]})")
    for text in (facts.get("other_income_sources") or []):
        low = text.lower()
        for needle, label in OUT_OF_SCOPE_PHRASES:
            if needle in low:
                out_of_scope.append(f"taxpayer statement: {label} ({text!r})")
                break
    out_of_scope = list(dict.fromkeys(out_of_scope))
    for item in out_of_scope:
        escalate.append({"kind": "out_of_scope", "reason": f"Out of scope for SilverFile: {item}.", "citation": "REF-SC-2"})
    det("Everything declared or uploaded is within the in-scope profile", not out_of_scope, "REF-SC-1" if not out_of_scope else "REF-SC-2")

    # 6. Conflicts between statements and documents (REF-ES-1 #2)
    declared = {k.upper() for k in facts.get("documents_declared", []) or []}
    uploaded = {(d.get("kind") or "").upper() for d in docs.values()}
    if state.get("extraction_done"):
        for k in sorted(declared - uploaded):
            if k in IN_SCOPE_KINDS:
                conflicts.append(f"Taxpayer said they have a {k} but none was supplied.")
        for k in sorted(uploaded - declared):
            if k in IN_SCOPE_KINDS and declared:
                conflicts.append(f"A {k} was supplied but the taxpayer did not mention one.")
        tp_last = (tp.get("last_name") or "").strip().lower()
        for d in docs.values():
            name = str(d.get("fields", {}).get("employee_name") or d.get("fields", {}).get("recipient_name") or "").lower()
            if tp_last and name and tp_last not in name and not (sp and (sp.get("last_name") or "").lower() in name):
                conflicts.append(f"Document {d.get('id')} is in the name {name!r}, which does not match the taxpayer or spouse.")
    for c in conflicts:
        escalate.append({"kind": "conflict", "reason": c, "citation": "REF-ES-1"})

    # 7. Third-party / exploitation signals surfaced by intake (REF-ES-1 #6)
    if facts.get("answering_party") == "third_party_no_authority":
        escalate.append({"kind": "exploitation", "reason": "A third party is answering on the taxpayer's behalf without a stated authority.", "citation": "REF-ES-1"})
    if facts.get("refund_account_is_own") is False:
        escalate.append({"kind": "exploitation", "reason": f"The refund would go to an account the taxpayer does not describe as their own ({facts.get('refund_account_description')!r}).", "citation": "REF-ES-1"})

    # 8. Which schedules apply
    schedules = ["Schedule 1-A"] if (boxes.get("taxpayer_65") or boxes.get("spouse_65")) else []
    if facts.get("schedule1_adjustments") or facts.get("schedule1_other_income"):
        schedules.append("Schedule 1")
    if facts.get("schedule2_line3") or facts.get("schedule2_line21"):
        schedules.append("Schedule 2")
    if facts.get("schedule3_line8"):
        schedules.append("Schedule 3")
    det("Schedules that apply", schedules, "REF-SC-1")
    if any(str(d.get("fields", {}).get("qualified_dividends") or 0) not in ("0", "0.0", "", "None") for d in docs.values() if (d.get("kind") or "").upper() == "1099-DIV"):
        det("Qualified dividends present: line 16 uses the rate schedule per the brief; preparer told", "note", "REF-TX-1")

    result = EligibilityResult(
        eligible_1040sr=eligible, filing_status=fs if fs_valid else None, filing_status_valid=fs_valid, boxes_12d=boxes, box_count=count,
        chart_applies=chart_applies, schedules=schedules, in_scope=not out_of_scope, out_of_scope_items=out_of_scope, conflicts=conflicts,
        citations=cites, determinations=dets, escalate=escalate,
    )
    new_escalations: list[Escalation] = []
    seen = {(e.get("kind"), e.get("reason")) for e in state.get("escalations", [])}
    for e in escalate:
        if (e["kind"], e["reason"]) not in seen:
            new_escalations.append(_esc(e["kind"], e["reason"], {"citation": e.get("citation")}))
    return {
        "eligibility": result,
        "escalations": new_escalations,
        "handoffs": ["eligibility"],
        "events": [f"eligibility: eligible={eligible} fs={fs} boxes={count} chart={chart_applies} scope_ok={not out_of_scope} escalations={len(new_escalations)}"],
    }
