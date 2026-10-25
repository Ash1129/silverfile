"""Pure, deterministic tax-year-2025 arithmetic for the in-scope 1040-SR profile.

Every function is total over its inputs, returns rounded dollars (2 dp) and
raises ValueError on inputs the spec does not cover. Constants come from the
SilverFile brief (section 3.2) and the 2025 rate schedules; each is tagged with
the reference-corpus id the Eligibility agent cites (see data/reference/).
"""
from __future__ import annotations

from dataclasses import dataclass, field

TAX_YEAR = 2025
FILING_STATUSES = ("single", "mfj", "mfs", "hoh", "qss")

# REF-SD-1: 2025 base standard deduction by filing status.
BASE_STANDARD_DEDUCTION = {"single": 15_750.0, "mfs": 15_750.0, "mfj": 31_500.0, "qss": 31_500.0, "hoh": 23_625.0}
# REF-SD-2: additional amount per box checked on line 12d.
ADDITIONAL_PER_BOX_UNMARRIED = 2_000.0  # single, hoh
ADDITIONAL_PER_BOX_OTHER = 1_600.0  # mfj, mfs, qss
# REF-SR-1: enhanced senior deduction (Schedule 1-A), tax years 2025-2028.
SENIOR_DEDUCTION_PER_PERSON = 6_000.0
SENIOR_PHASEOUT_THRESHOLD = {"single": 75_000.0, "mfs": 75_000.0, "hoh": 75_000.0, "qss": 75_000.0, "mfj": 150_000.0}
SENIOR_PHASEOUT_RATE = 0.06
# REF-SS-1: taxable Social Security worksheet thresholds.
SS_BASE = {"single": 25_000.0, "hoh": 25_000.0, "qss": 25_000.0, "mfs": 25_000.0, "mfj": 32_000.0}
SS_SECOND = {"single": 34_000.0, "hoh": 34_000.0, "qss": 34_000.0, "mfs": 34_000.0, "mfj": 44_000.0}
# REF-EL-1: 1040-SR age test for tax year 2025.
BORN_BEFORE = "1961-01-02"

# REF-TX-1: 2025 rate schedules (upper bound of each bracket, rate).
RATE_SCHEDULE = {
    "single": [(11_925, 0.10), (48_475, 0.12), (103_350, 0.22), (197_300, 0.24), (250_525, 0.32), (626_350, 0.35), (None, 0.37)],
    "mfs": [(11_925, 0.10), (48_475, 0.12), (103_350, 0.22), (197_300, 0.24), (250_525, 0.32), (375_800, 0.35), (None, 0.37)],
    "mfj": [(23_850, 0.10), (96_950, 0.12), (206_700, 0.22), (394_600, 0.24), (501_050, 0.32), (751_600, 0.35), (None, 0.37)],
    "qss": [(23_850, 0.10), (96_950, 0.12), (206_700, 0.22), (394_600, 0.24), (501_050, 0.32), (751_600, 0.35), (None, 0.37)],
    "hoh": [(17_000, 0.10), (64_850, 0.12), (103_350, 0.22), (197_300, 0.24), (250_525, 0.32), (626_350, 0.35), (None, 0.37)],
}


def _money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _status(fs: str) -> str:
    fs = (fs or "").lower()
    if fs not in FILING_STATUSES:
        raise ValueError(f"unknown filing status {fs!r}; expected one of {FILING_STATUSES}")
    return fs


def sum_amounts(amounts: list[float]) -> float:
    """Line totals: 1z from several W-2s, 25d, 9, 33 and so on."""
    return _money(sum(float(a) for a in amounts))


def subtract(a: float, b: float) -> float:
    return _money(float(a) - float(b))


def nonnegative_difference(a: float, b: float) -> float:
    """max(0, a - b): taxable income, amount owed, overpayment."""
    return _money(max(0.0, float(a) - float(b)))


def is_1040sr_eligible(taxpayer_dob: str, spouse_dob: str | None = None, filing_status: str = "single") -> dict:
    """REF-EL-1: born before 2 January 1961; on a joint return one spouse suffices."""
    fs = _status(filing_status)
    tp = taxpayer_dob < BORN_BEFORE
    sp = bool(spouse_dob) and spouse_dob < BORN_BEFORE
    eligible = tp or (fs == "mfj" and sp)
    return {"eligible": eligible, "taxpayer_meets_age": tp, "spouse_meets_age": sp, "rule": "REF-EL-1"}


def is_65_or_older(dob: str) -> bool:
    """For the line 12d age box in tax year 2025: born before 2 January 1961 (REF-SD-3)."""
    return bool(dob) and dob < BORN_BEFORE


def count_12d_boxes(taxpayer_dob: str, taxpayer_blind: bool, spouse_dob: str | None, spouse_blind: bool, filing_status: str) -> dict:
    """REF-SD-3: one box each for taxpayer 65+, taxpayer blind, spouse 65+, spouse blind.

    Spouse boxes count only on a joint return (and on MFS when the spouse had no
    income and is not filed for by anyone else, which is out of this profile, so
    MFS counts taxpayer boxes only).
    """
    fs = _status(filing_status)
    boxes = {"taxpayer_65": is_65_or_older(taxpayer_dob), "taxpayer_blind": bool(taxpayer_blind), "spouse_65": False, "spouse_blind": False}
    if fs == "mfj":
        boxes["spouse_65"] = is_65_or_older(spouse_dob or "9999-12-31")
        boxes["spouse_blind"] = bool(spouse_blind)
    return {"boxes": boxes, "count": sum(1 for v in boxes.values() if v), "rule": "REF-SD-3"}


def standard_deduction(filing_status: str, boxes_checked: int, chart_applies: bool = True) -> dict:
    """REF-SD-1/REF-SD-2: Standard Deduction Chart on the 1040-SR."""
    fs = _status(filing_status)
    if not chart_applies:
        raise ValueError("Standard Deduction Chart does not apply (dependent, itemising spouse or dual-status alien): escalate")
    if boxes_checked < 0 or boxes_checked > 4:
        raise ValueError("boxes_checked must be 0..4")
    per_box = ADDITIONAL_PER_BOX_UNMARRIED if fs in ("single", "hoh") else ADDITIONAL_PER_BOX_OTHER
    base = BASE_STANDARD_DEDUCTION[fs]
    return {"amount": _money(base + per_box * boxes_checked), "base": base, "per_box": per_box, "boxes": boxes_checked, "rule": "REF-SD-1"}


def senior_deduction(filing_status: str, magi: float, taxpayer_65: bool, spouse_65: bool = False) -> dict:
    """REF-SR-1: $6,000 per qualifying person 65+, phased out at 6% of MAGI over the threshold, per person.

    Separate from, and in addition to, the additional standard deduction.
    """
    fs = _status(filing_status)
    qualifying = int(bool(taxpayer_65)) + (int(bool(spouse_65)) if fs == "mfj" else 0)
    threshold = SENIOR_PHASEOUT_THRESHOLD[fs]
    excess = max(0.0, float(magi) - threshold)
    reduction_each = SENIOR_PHASEOUT_RATE * excess
    per_person = max(0.0, SENIOR_DEDUCTION_PER_PERSON - reduction_each)
    return {
        "amount": _money(per_person * qualifying), "qualifying_persons": qualifying, "threshold": threshold,
        "excess_magi": _money(excess), "reduction_per_person": _money(min(reduction_each, SENIOR_DEDUCTION_PER_PERSON)), "rule": "REF-SR-1",
    }


def provisional_income(agi_excluding_benefits: float, tax_exempt_interest: float, total_benefits: float) -> float:
    """REF-SS-1 step 1: AGI without benefits + tax-exempt interest + half of benefits."""
    return _money(float(agi_excluding_benefits) + float(tax_exempt_interest) + 0.5 * float(total_benefits))


def taxable_social_security(filing_status: str, total_benefits: float, agi_excluding_benefits: float, tax_exempt_interest: float, lived_with_spouse_mfs: bool = False) -> dict:
    """REF-SS-1: the Social Security Benefits Worksheet, simplified to the in-scope profile.

    Depends on every other income line being final first (ordering dependency).
    """
    fs = _status(filing_status)
    b = float(total_benefits)
    if b <= 0:
        return {"taxable": 0.0, "provisional_income": provisional_income(agi_excluding_benefits, tax_exempt_interest, 0), "tier": "none", "rule": "REF-SS-1"}
    pi = provisional_income(agi_excluding_benefits, tax_exempt_interest, b)
    if fs == "mfs" and lived_with_spouse_mfs:
        taxable = 0.85 * b
        tier = "mfs-lived-together"
    else:
        base, second = SS_BASE[fs], SS_SECOND[fs]
        if pi <= base:
            taxable, tier = 0.0, "below-base"
        elif pi <= second:
            taxable, tier = min(0.5 * b, 0.5 * (pi - base)), "first-tier"
        else:
            first_tier_cap = min(0.5 * b, 0.5 * (second - base))
            taxable, tier = min(0.85 * b, 0.85 * (pi - second) + first_tier_cap), "second-tier"
    return {"taxable": _money(taxable), "provisional_income": pi, "tier": tier, "rule": "REF-SS-1"}


def taxable_1099r(gross: float, taxable_amount: float | None, taxable_not_determined: bool, qcd_amount: float = 0.0, is_ira: bool = False) -> dict:
    """REF-RT-1: taxable portion of a 1099-R in the in-scope profile.

    Box 2b 'taxable amount not determined' is out of scope (Simplified Method /
    General Rule) and raises so the caller escalates. A qualified charitable
    distribution reduces the taxable IRA amount.
    """
    if taxable_not_determined:
        raise ValueError("1099-R box 2b 'taxable amount not determined' is checked: out of scope, escalate")
    g = float(gross)
    t = g if taxable_amount is None else float(taxable_amount)
    if is_ira and qcd_amount:
        t = max(0.0, t - float(qcd_amount))
    if t > g:
        raise ValueError("taxable amount exceeds gross distribution")
    return {"gross": _money(g), "taxable": _money(t), "qcd": _money(qcd_amount or 0), "rule": "REF-RT-1"}


def income_tax(filing_status: str, taxable_income: float) -> dict:
    """REF-TX-1: 2025 rate schedule applied to taxable income (line 16).

    Per the brief, line 16 comes from the rate schedule; the qualified dividends
    and capital gain worksheet is not applied. The preparer is told so.
    """
    fs = _status(filing_status)
    ti = max(0.0, float(taxable_income))
    tax, lower, rows = 0.0, 0.0, []
    for upper, rate in RATE_SCHEDULE[fs]:
        top = ti if upper is None else min(ti, float(upper))
        if top > lower:
            part = (top - lower) * rate
            rows.append({"from": lower, "to": top, "rate": rate, "tax": _money(part)})
            tax += part
        if upper is None or ti <= upper:
            break
        lower = float(upper)
    return {"tax": _money(tax), "brackets": rows, "rule": "REF-TX-1"}


def settle(total_tax: float, total_payments: float) -> dict:
    """Lines 34 and 37."""
    over = nonnegative_difference(total_payments, total_tax)
    owed = nonnegative_difference(total_tax, total_payments)
    return {"overpaid": over, "amount_owed": owed}


TOOL_FUNCTIONS = {
    "sum_amounts": sum_amounts,
    "subtract": subtract,
    "nonnegative_difference": nonnegative_difference,
    "is_1040sr_eligible": is_1040sr_eligible,
    "count_12d_boxes": count_12d_boxes,
    "standard_deduction": standard_deduction,
    "senior_deduction": senior_deduction,
    "provisional_income": provisional_income,
    "taxable_social_security": taxable_social_security,
    "taxable_1099r": taxable_1099r,
    "income_tax": income_tax,
    "settle": settle,
}
