"""Computation agent.

Populates every derived line of the draft 1040-SR. It never does arithmetic
itself: each number is the result of a tax_math tool call (over MCP stdio by
default) and gets a provenance record with the tool, its inputs and the source
documents. The ordering dependency the brief points at is explicit below: the
Social Security worksheet (line 6b) runs only after every other income line is
final, and its result then feeds line 9 and AGI.
"""
from __future__ import annotations

from typing import Any

from ..comms.bus import latest_request
from ..faults import apply_computation_faults
from ..provenance import record, user_input
from ..state import Provenance, ReturnState
from ..tax_math.client import TaxMathClient
from ..security.validate import unknown_fact_actions
from ..provenance import now_iso
from ..reconciliation import active_documents, evidence_conflicts


def _num(v: Any) -> float:
    if v in (None, "", "None"):
        raise ValueError("An unknown amount cannot be used as zero.")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        raise ValueError("An unreadable amount must be confirmed before computation.") from None


def _docs_of(state: ReturnState, kind: str) -> list[dict[str, Any]]:
    return [d for d in active_documents(state).values() if (d.get("kind") or "").upper() == kind and not d.get("out_of_scope")]


def run_computation(state: ReturnState) -> dict[str, Any]:
    conflicts = evidence_conflicts(state)
    if conflicts:
        return {"escalations": [{"id": "esc-computation-conflict", "kind": "conflict", "reason": "Source-linked monetary conflicts remain unresolved.", "raised_by": "computation", "raised_at": now_iso(), "details": {"conflicts": conflicts}, "status": "open"}], "handoffs": ["computation"], "events": ["computation blocked: conflicting evidence"]}
    unknown = unknown_fact_actions(state, require_chart_facts=True)
    if unknown:
        return {"escalations": [{"id": "esc-computation-unknown", "kind": "uncertain_fact", "reason": "Required computation inputs are unknown.", "raised_by": "computation", "raised_at": now_iso(), "details": {"required_actions": unknown}, "status": "open"}], "handoffs": ["computation"], "events": ["computation blocked: required facts are unknown"]}
    facts = state.get("intake", {}) or {}
    elig = state.get("eligibility", {}) or {}
    fs = elig.get("filing_status") or facts.get("filing_status") or "single"
    entered_by = "taxpayer" if facts.get("answering_party") in (None, "self", "unknown") else "third party (with stated authority)"
    fault = state.get("fault_injection") or {}
    version = int(state.get("computation_version", 0))  # assigned by the orchestrator
    lines: dict[str, float] = {}
    prov: dict[str, Provenance] = {}
    # review findings arrive as work orders inside the orchestrator's request; record which ones this run answers
    orders = ((latest_request(state, "computation") or {}).get("body") or {}).get("work_orders") or []
    notes: list[str] = [f"re-run for review work order {o['finding_id']}: {o['detail']}" for o in orders]

    with TaxMathClient() as tm:
        def put(line: str, amount: float, **kw: Any) -> float:
            lines[line] = float(amount)  # tools return cents-rounded values already
            if kw.get("source_kind") == "document":
                kw["inputs"] = {**kw.get("inputs", {}), "document_versions": {key: state["extracted"][key].get("version") for key in kw.get("sources", [])}}
            prov[line] = record(line, amount, **kw)
            return lines[line]

        def call(tool: str, **args: Any) -> Any:
            return tm.call(tool, **args)

        def last() -> str:
            return tm.calls[-1].transport if tm.calls else "n/a"

        # ---- income lines straight from documents -------------------------
        w2s = _docs_of(state, "W-2")
        wages = [_num(d["fields"].get("wages")) for d in w2s]
        put("1z", call("sum_amounts", amounts=wages), source_kind="document", sources=[d["id"] for d in w2s], method="tax_math.sum_amounts", inputs={"amounts": wages, "boxes": ["Box 1" for _ in w2s]}, transport=last(), citations=["REF-SC-1"])
        w2_wh = [_num(d["fields"].get("federal_withheld")) for d in w2s]
        put("25a", call("sum_amounts", amounts=w2_wh), source_kind="document", sources=[d["id"] for d in w2s], method="tax_math.sum_amounts", inputs={"amounts": w2_wh, "boxes": ["Box 2" for _ in w2s]}, transport=last())

        ints = _docs_of(state, "1099-INT")
        ti = [_num(d["fields"].get("interest_income")) for d in ints]
        te = [_num(d["fields"].get("tax_exempt_interest")) for d in ints]
        put("2b", call("sum_amounts", amounts=ti), source_kind="document", sources=[d["id"] for d in ints], method="tax_math.sum_amounts", inputs={"amounts": ti, "boxes": ["Box 1" for _ in ints]}, transport=last())
        put("2a", call("sum_amounts", amounts=te), source_kind="document", sources=[d["id"] for d in ints], method="tax_math.sum_amounts", inputs={"amounts": te, "boxes": ["Box 8" for _ in ints]}, transport=last())

        divs = _docs_of(state, "1099-DIV")
        od = [_num(d["fields"].get("ordinary_dividends")) for d in divs]
        qd = [_num(d["fields"].get("qualified_dividends")) for d in divs]
        put("3b", call("sum_amounts", amounts=od), source_kind="document", sources=[d["id"] for d in divs], method="tax_math.sum_amounts", inputs={"amounts": od, "boxes": ["Box 1a" for _ in divs]}, transport=last())
        put("3a", call("sum_amounts", amounts=qd), source_kind="document", sources=[d["id"] for d in divs], method="tax_math.sum_amounts", inputs={"amounts": qd, "boxes": ["Box 1b" for _ in divs]}, transport=last())
        if lines["3a"]:
            notes.append("Line 3a (qualified dividends) is non-zero. Per the brief, line 16 uses the rate schedule; the preparer should check whether the qualified dividends worksheet changes the tax (REF-TX-1).")

        rs = _docs_of(state, "1099-R")
        ira_tot, ira_tax, pen_tot, pen_tax, r_wh, r_ids = [], [], [], [], [], []
        for d in rs:
            f = d["fields"]
            is_ira = bool(f.get("ira_sep_simple")) or str(f.get("account_type", "")).lower() == "ira"
            r = call("taxable_1099r", gross=_num(f.get("gross_distribution")), taxable_amount=(None if f.get("taxable_amount") in (None, "") else _num(f.get("taxable_amount"))), taxable_not_determined=bool(f.get("taxable_not_determined")), qcd_amount=_num(f.get("qcd_amount")), is_ira=is_ira)
            (ira_tot if is_ira else pen_tot).append(r["gross"])
            (ira_tax if is_ira else pen_tax).append(r["taxable"])
            r_wh.append(_num(f.get("federal_withheld")))
            r_ids.append(d["id"])
        put("4a", call("sum_amounts", amounts=ira_tot), source_kind="document", sources=r_ids, method="tax_math.sum_amounts", inputs={"amounts": ira_tot, "boxes": ["Box 1 (IRA)" for _ in ira_tot]}, transport=last(), citations=["REF-RT-1"])
        put("4b", call("sum_amounts", amounts=ira_tax), source_kind="document", sources=r_ids, method="tax_math.taxable_1099r + sum_amounts", inputs={"amounts": ira_tax}, transport=last(), citations=["REF-RT-1"])
        put("5a", call("sum_amounts", amounts=pen_tot), source_kind="document", sources=r_ids, method="tax_math.sum_amounts", inputs={"amounts": pen_tot, "boxes": ["Box 1 (pension)" for _ in pen_tot]}, transport=last(), citations=["REF-RT-1"])
        put("5b", call("sum_amounts", amounts=pen_tax), source_kind="document", sources=r_ids, method="tax_math.taxable_1099r + sum_amounts", inputs={"amounts": pen_tax}, transport=last(), citations=["REF-RT-1"])

        ssas = _docs_of(state, "SSA-1099")
        ben = [_num(d["fields"].get("net_benefits")) for d in ssas]
        ssa_wh = [_num(d["fields"].get("federal_withheld")) for d in ssas]
        put("6a", call("sum_amounts", amounts=ben), source_kind="document", sources=[d["id"] for d in ssas], method="tax_math.sum_amounts", inputs={"amounts": ben, "boxes": ["Box 5" for _ in ssas]}, transport=last(), citations=["REF-SS-1"])
        put("25b", call("sum_amounts", amounts=[*r_wh, *ssa_wh]), source_kind="document", sources=[*r_ids, *[d["id"] for d in ssas]], method="tax_math.sum_amounts", inputs={"amounts": [*r_wh, *ssa_wh], "boxes": [*["Box 4 (1099-R)" for _ in r_wh], *["Box 6 (SSA-1099)" for _ in ssa_wh]]}, transport=last())
        put("25c", 0.0, source_kind="computed", sources=[], method="none in profile", transport="n/a")

        # ---- direct user inputs (Schedules 1, 2, 3; estimated payments) -----
        for line, key in (("8", "schedule1_other_income"), ("10", "schedule1_adjustments"), ("17", "schedule2_line3"), ("23", "schedule2_line21"), ("20", "schedule3_line8"), ("26", "estimated_payments")):
            v = _num(facts.get(key, 0.0))  # absent, unclaimed optional schedules; explicit unknowns are blocked above
            lines[line] = call("sum_amounts", amounts=[v])  # normalised to cents by the tool, not here
            edit = state.get("human_edit") or {}
            source_actor = f"staff:{edit['completed_by']}" if key in (edit.get("facts_patch") or {}) and edit.get("completed_by") else entered_by
            prov[line] = user_input(line, v, source_actor, note=None if key in facts else "No optional claim supplied for this profile.")
            if source_actor != entered_by:
                prov[line]["entered_at"] = edit.get("completed_at")
        lines["7"] = 0.0
        prov["7"] = record("7", 0.0, source_kind="computed", sources=[], method="out of scope (Schedule D); 0 by profile", citations=["REF-SC-2"])
        lines["19"] = 0.0
        prov["19"] = record("19", 0.0, source_kind="computed", sources=[], method="no credit claimed in profile")

        # ---- ORDERING DEPENDENCY: income excluding benefits -> worksheet -> 6b ---
        income_ex_ss = call("sum_amounts", amounts=[lines["1z"], lines["2b"], lines["3b"], lines["4b"], lines["5b"], lines["7"], lines["8"]])
        agi_ex_ss = call("subtract", a=income_ex_ss, b=lines["10"])
        ss = call("taxable_social_security", filing_status=fs, total_benefits=lines["6a"], agi_excluding_benefits=agi_ex_ss, tax_exempt_interest=lines["2a"], lived_with_spouse_mfs=False)
        put("6b", ss["taxable"], source_kind="worksheet", sources=["6a", "2a", "1z", "2b", "3b", "4b", "5b", "8", "10"], method="tax_math.taxable_social_security", inputs={"filing_status": fs, "total_benefits": lines["6a"], "agi_excluding_benefits": agi_ex_ss, "tax_exempt_interest": lines["2a"], "provisional_income": ss["provisional_income"], "tier": ss["tier"]}, transport=last(), citations=["REF-SS-1"])

        put("9", call("sum_amounts", amounts=[lines["1z"], lines["2b"], lines["3b"], lines["4b"], lines["5b"], lines["6b"], lines["7"], lines["8"]]), source_kind="computed", sources=["1z", "2b", "3b", "4b", "5b", "6b", "7", "8"], method="tax_math.sum_amounts", inputs={"lines": ["1z", "2b", "3b", "4b", "5b", "6b", "7", "8"]}, transport=last())
        put("11a", call("subtract", a=lines["9"], b=lines["10"]), source_kind="computed", sources=["9", "10"], method="tax_math.subtract", inputs={"a": lines["9"], "b": lines["10"]}, transport=last())

        # ---- deductions --------------------------------------------------
        boxes = elig.get("boxes_12d") or {}
        count = int(elig.get("box_count") or 0)
        lines["12d"] = float(count)
        tp_ = (state.get("intake") or {}).get("taxpayer") or {}
        sp_ = (state.get("intake") or {}).get("spouse") or {}
        prov["12d"] = record("12d", count, source_kind="chart", sources=["intake"], method="tax_math.count_12d_boxes",
                             inputs={**dict(boxes), "taxpayer_dob": tp_.get("dob"), "spouse_dob": sp_.get("dob"), "age_test": "born before 1961-01-02"}, citations=["REF-SD-3"])
        sd = call("standard_deduction", filing_status=fs, boxes_checked=count, chart_applies=bool(elig.get("chart_applies", True)))
        put("12e", sd["amount"], source_kind="chart", sources=["12d"], method="tax_math.standard_deduction", inputs={"filing_status": fs, "boxes_checked": count, "base": sd["base"], "per_box": sd["per_box"]}, transport=last(), citations=["REF-SD-1", "REF-SD-2"])
        sr = call("senior_deduction", filing_status=fs, magi=lines["11a"], taxpayer_65=bool(boxes.get("taxpayer_65")), spouse_65=bool(boxes.get("spouse_65")))
        put("13b", sr["amount"], source_kind="computed", sources=["11a", "12d"], method="tax_math.senior_deduction", inputs={"filing_status": fs, "magi": lines["11a"], "qualifying_persons": sr["qualifying_persons"], "threshold": sr["threshold"], "excess_magi": sr["excess_magi"], "reduction_per_person": sr["reduction_per_person"]}, transport=last(), citations=["REF-SR-1"])
        put("14", call("sum_amounts", amounts=[lines["12e"], lines["13b"]]), source_kind="computed", sources=["12e", "13b"], method="tax_math.sum_amounts", inputs={"lines": ["12e", "13b"]}, transport=last())
        put("15", call("nonnegative_difference", a=lines["11a"], b=lines["14"]), source_kind="computed", sources=["11a", "14"], method="tax_math.nonnegative_difference", inputs={"a": lines["11a"], "b": lines["14"]}, transport=last())

        # ---- tax ---------------------------------------------------------
        tx = call("income_tax", filing_status=fs, taxable_income=lines["15"])
        put("16", tx["tax"], source_kind="rate-schedule", sources=["15"], method="tax_math.income_tax", inputs={"filing_status": fs, "taxable_income": lines["15"], "brackets": tx["brackets"]}, transport=last(), citations=["REF-TX-1"])
        put("18", call("sum_amounts", amounts=[lines["16"], lines["17"]]), source_kind="computed", sources=["16", "17"], method="tax_math.sum_amounts", inputs={"lines": ["16", "17"]}, transport=last())
        put("21", call("sum_amounts", amounts=[lines["19"], lines["20"]]), source_kind="computed", sources=["19", "20"], method="tax_math.sum_amounts", inputs={"lines": ["19", "20"]}, transport=last())
        put("22", call("nonnegative_difference", a=lines["18"], b=lines["21"]), source_kind="computed", sources=["18", "21"], method="tax_math.nonnegative_difference", inputs={"a": lines["18"], "b": lines["21"]}, transport=last())
        put("24", call("sum_amounts", amounts=[lines["22"], lines["23"]]), source_kind="computed", sources=["22", "23"], method="tax_math.sum_amounts", inputs={"lines": ["22", "23"]}, transport=last())

        # ---- payments ----------------------------------------------------
        put("25d", call("sum_amounts", amounts=[lines["25a"], lines["25b"], lines["25c"]]), source_kind="computed", sources=["25a", "25b", "25c"], method="tax_math.sum_amounts", inputs={"lines": ["25a", "25b", "25c"]}, transport=last())
        lines["32"] = 0.0
        prov["32"] = record("32", 0.0, source_kind="computed", sources=[], method="no refundable credits in profile")
        put("33", call("sum_amounts", amounts=[lines["25d"], lines["26"], lines["32"]]), source_kind="computed", sources=["25d", "26", "32"], method="tax_math.sum_amounts", inputs={"lines": ["25d", "26", "32"]}, transport=last())
        st = call("settle", total_tax=lines["24"], total_payments=lines["33"])
        put("34", st["overpaid"], source_kind="computed", sources=["33", "24"], method="tax_math.settle", inputs={"total_tax": lines["24"], "total_payments": lines["33"]}, transport=last())
        put("37", st["amount_owed"], source_kind="computed", sources=["24", "33"], method="tax_math.settle", inputs={"total_tax": lines["24"], "total_payments": lines["33"]}, transport=last())

        transports = {c.transport for c in tm.calls}
        n_calls = len(tm.calls)

    apply_computation_faults(fault, version, lines, prov, notes)  # test-only; see faults.py

    return {
        "lines": lines,
        "provenance": prov,
        "computation_version": version,
        "computation_notes": notes,
        "handoffs": ["computation"],
        "events": [f"computation v{version}: {n_calls} tax_math calls over {sorted(transports)}; taxable income {lines['15']}, tax {lines['16']}, owed {lines['37']}, overpaid {lines['34']}"],
    }
