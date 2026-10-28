"""Generate the synthetic test data SilverFile ships with.

- data/fixtures/<case>/*.json: typed document payloads (SSA-1099, 1099-R, 1099-INT, 1099-DIV, W-2 sidecars)
- data/fixtures/<case>/intake.json: scripted intake answers for `run.py demo`
- demo/w2_image_1.jpg, demo/w2_image_2.jpg: drawn W-2 images the Extraction agent reads with vision

All names, employers and numbers are invented. SSNs use the 900 test series.
Run: python data/generate_fixtures.py
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "data" / "fixtures"
DEMO = ROOT / "demo"


def font(size: int):
    for name in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_w2(path: Path, w2: dict) -> None:
    """A clean, large-type W-2 layout with the standard box letters and numbers."""
    W, H = 1600, 1100
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    big, med, small = font(34), font(26), font(20)
    d.rectangle([20, 20, W - 20, H - 20], outline="black", width=3)
    d.text((40, 30), f"Form W-2  Wage and Tax Statement  {w2['tax_year']}", font=big, fill="black")
    d.text((W - 520, 36), "Copy B — To Be Filed With Employee's FEDERAL Tax Return", font=small, fill="black")

    def box(x0, y0, x1, y1, label, value):
        d.rectangle([x0, y0, x1, y1], outline="black", width=2)
        d.text((x0 + 8, y0 + 6), label, font=small, fill="black")
        d.text((x0 + 12, y0 + 40), value, font=med, fill="black")

    left = 40
    box(left, 90, 780, 170, "a  Employee's social security number", w2["employee_ssn"])
    box(left, 180, 780, 260, "b  Employer identification number (EIN)", w2["employer_ein"])
    box(left, 270, 780, 420, "c  Employer's name, address, and ZIP code", w2["employer_name"] + "\n" + w2["employer_address"])
    box(left, 430, 780, 580, "e/f  Employee's name, address, and ZIP code", w2["employee_name"] + "\n" + w2["employee_address"])
    right = 800
    rows = [("1  Wages, tips, other compensation", w2["wages"], "2  Federal income tax withheld", w2["federal_withheld"]),
            ("3  Social security wages", w2["social_security_wages"], "4  Social security tax withheld", w2["social_security_tax"]),
            ("5  Medicare wages and tips", w2["medicare_wages"], "6  Medicare tax withheld", w2["medicare_tax"]),
            ("7  Social security tips", "", "8  Allocated tips", ""),
            ("9", "", "10  Dependent care benefits", ""),
            ("11  Nonqualified plans", "", "12a  See instructions for box 12", "")]
    y = 90
    for l1, v1, l2, v2 in rows:
        box(right, y, right + 370, y + 80, l1, v1)
        box(right + 380, y, right + 760, y + 80, l2, v2)
        y += 90
    box(left, 600, 780, 680, "d  Control number", w2.get("control_number", ""))
    box(left, 700, 780, 780, "15  State / Employer's state ID number", f"{w2['state']}  {w2['state_id']}")
    box(left, 790, 400, 870, "16  State wages, tips, etc.", w2["wages"])
    box(410, 790, 780, 870, "17  State income tax", w2["state_withheld"])
    d.text((40, H - 90), "Department of the Treasury — Internal Revenue Service. SYNTHETIC TEST DOCUMENT for the SilverFile demo; not a real form.", font=small, fill="black")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=92)


CASES = {
    "case1_clean": {
        "intake": {
            "answering_party": "self", "taxpayer": {"first_name": "Eleanor", "last_name": "Whitfield", "ssn": "912-34-5678", "dob": "1954-03-14", "blind": False},
            "spouse": None, "married": False, "spouse_died_in_year": False, "filing_status": "single", "dependents": [], "can_be_claimed_as_dependent": False,
            "documents_declared": ["W-2", "SSA-1099", "1099-R", "1099-INT"], "other_income_sources": [], "estimated_payments": 500.0, "schedule1_adjustments": 0.0,
            "schedule1_other_income": 0.0, "schedule2_line3": 0.0, "schedule3_line8": 0.0, "refund_account_description": "my checking account at Finger Lakes Savings", "refund_account_is_own": True,
            "noted_questions": [], "volunteered_ssn_events": [], "_uploaded": True, "_confirmed": True, "_dependents_done": True,
        },
        "answers": ["Yes, it's my return", "Eleanor Whitfield", "912-34-5678", "March 14, 1954", "No", "Single", "Single", "None", "No", "I have a W-2 from the library, my SSA-1099, a 1099-R from my pension, and a 1099-INT from the bank", "No", "500", "None", "None", "None", "None", "My checking account at Finger Lakes Savings", "Yes", "done", "Yes"],
        "w2": {"tax_year": "2025", "employee_ssn": "912-34-5678", "employer_ein": "16-1234567", "employer_name": "Tompkins Public Library", "employer_address": "101 E Green St, Ithaca, NY 14850", "employee_name": "Eleanor M Whitfield", "employee_address": "22 Cayuga Heights Rd, Ithaca, NY 14850", "wages": "18,400.00", "federal_withheld": "1,210.00", "social_security_wages": "18,400.00", "social_security_tax": "1,140.80", "medicare_wages": "18,400.00", "medicare_tax": "266.80", "state": "NY", "state_id": "16-1234567", "state_withheld": "640.00", "control_number": "000417"},
        "docs": {
            "ssa1099": {"kind": "SSA-1099", "recipient_name": "Eleanor M Whitfield", "net_benefits": 21600.00, "federal_withheld": 0.0, "tax_year": "2025"},
            "1099int": {"kind": "1099-INT", "recipient_name": "Eleanor M Whitfield", "payer_name": "Finger Lakes Savings", "interest_income": 842.17, "federal_withheld": 0.0, "tax_exempt_interest": 0.0, "tax_year": "2025"},
            "1099r": {"kind": "1099-R", "recipient_name": "Eleanor M Whitfield", "payer_name": "Tompkins County Retirement System", "gross_distribution": 14200.00, "taxable_amount": 14200.00, "taxable_not_determined": False, "federal_withheld": 900.0, "distribution_code": "7", "ira_sep_simple": False, "tax_year": "2025"},
        },
    },
    "case2_hitl": {
        "intake": {
            "answering_party": "self", "taxpayer": {"first_name": "Harold", "last_name": "Benson", "ssn": "923-45-6789", "dob": "1949-11-02", "blind": True},
            "spouse": {"first_name": "Margaret", "last_name": "Benson", "ssn": "945-67-8901", "dob": "1951-06-21", "blind": False, "date_of_death": "2025-05-09"}, "married": True, "spouse_died_in_year": True, "filing_status": None, "_filing_status_unsure": True,
            "dependents": [], "can_be_claimed_as_dependent": False, "documents_declared": ["SSA-1099", "1099-R", "1099-DIV"], "other_income_sources": [], "estimated_payments": 0.0, "schedule1_adjustments": 0.0,
            "schedule1_other_income": 0.0, "schedule2_line3": 0.0, "schedule3_line8": 0.0, "refund_account_description": "our joint checking at Cayuga Trust", "refund_account_is_own": True,
            "noted_questions": [], "volunteered_ssn_events": [], "_uploaded": True, "_confirmed": True, "_dependents_done": True,
        },
        "answers": ["Yes", "Harold Benson", "923-45-6789", "11/02/1949", "Yes, I am legally blind", "Widowed", "Yes, in May", "Margaret Benson", "945-67-8901", "June 21, 1951", "No", "None", "No", "SSA-1099, a 1099-R from my pension, and a 1099-DIV", "No", "None", "None", "None", "None", "None", "Our joint checking at Cayuga Trust", "Yes", "done", "Yes"],
        "w2": None,
        "docs": {
            "ssa1099": {"kind": "SSA-1099", "recipient_name": "Harold J Benson", "net_benefits": 26400.00, "federal_withheld": 1200.0, "tax_year": "2025"},
            "1099r": {"kind": "1099-R", "recipient_name": "Harold J Benson", "payer_name": "NY State Teachers' Retirement", "gross_distribution": 31200.00, "taxable_amount": 31200.00, "taxable_not_determined": False, "federal_withheld": 2800.0, "distribution_code": "7", "ira_sep_simple": False, "tax_year": "2025"},
            "1099div": {"kind": "1099-DIV", "recipient_name": "Harold J Benson", "payer_name": "Vanguard", "ordinary_dividends": 1320.55, "qualified_dividends": 1100.00, "federal_withheld": 0.0, "tax_year": "2025"},
        },
    },
    "case3_out_of_scope": {
        "intake": None, "answers": None, "w2": None,
        "docs": {
            "ssa1099": {"kind": "SSA-1099", "recipient_name": "Ruth Adler", "net_benefits": 19800.00, "federal_withheld": 0.0, "tax_year": "2025"},
            "1099nec": {"kind": "1099-NEC", "recipient_name": "Ruth Adler", "payer_name": "Quilting Guild LLC", "nonemployee_compensation": 4300.00, "tax_year": "2025"},
        },
    },
    "case4_r_box2b": {
        "intake": None, "answers": None, "w2": None,
        "docs": {
            "1099r": {"kind": "1099-R", "recipient_name": "Samuel Ortega", "payer_name": "Acme Annuity Co", "gross_distribution": 14000.00, "taxable_amount": None, "taxable_not_determined": True, "federal_withheld": 0.0, "distribution_code": "7", "ira_sep_simple": False, "tax_year": "2025"},
        },
    },
    "batch_w2s": {"intake": None, "answers": None, "w2": None, "docs": {}},
}


def main() -> None:
    for case, spec in CASES.items():
        d = FIX / case
        d.mkdir(parents=True, exist_ok=True)
        if spec.get("intake") is not None:
            (d / "intake.json").write_text(json.dumps(spec["intake"], indent=1))
        if spec.get("answers") is not None:
            (d / "answers.json").write_text(json.dumps(spec["answers"], indent=1))
        for name, doc in spec["docs"].items():
            (d / f"{name}.json").write_text(json.dumps(doc, indent=1))
    # demo W-2 images
    w2 = CASES["case1_clean"]["w2"]
    draw_w2(DEMO / "w2_image_1.jpg", w2)
    (DEMO / "w2_image_1.json").write_text(json.dumps({"kind": "W-2", "employee_name": w2["employee_name"], "employee_ssn_last4": w2["employee_ssn"][-4:], "employer_name": w2["employer_name"], "employer_ein": w2["employer_ein"], "wages": 18400.00, "federal_withheld": 1210.00, "social_security_wages": 18400.00, "social_security_tax": 1140.80, "medicare_wages": 18400.00, "medicare_tax": 266.80, "tax_year": "2025"}, indent=1))
    w2b = {**w2, "employer_name": "Cornell Botanic Gardens", "employer_ein": "15-9876543", "employer_address": "124 Comstock Knoll Dr, Ithaca, NY 14850", "wages": "6,250.00", "federal_withheld": "310.00", "social_security_wages": "6,250.00", "social_security_tax": "387.50", "medicare_wages": "6,250.00", "medicare_tax": "90.63", "state_withheld": "190.00", "control_number": "000982"}
    draw_w2(DEMO / "w2_image_2.jpg", w2b)
    (DEMO / "w2_image_2.json").write_text(json.dumps({"kind": "W-2", "employee_name": w2b["employee_name"], "employee_ssn_last4": "5678", "employer_name": w2b["employer_name"], "employer_ein": w2b["employer_ein"], "wages": 6250.00, "federal_withheld": 310.00, "social_security_wages": 6250.00, "social_security_tax": 387.50, "medicare_wages": 6250.00, "medicare_tax": 90.63, "tax_year": "2025"}, indent=1))
    # ten W-2 images for the batch / context-window scenario
    for i in range(10):
        wi = {**w2, "employer_name": f"Employer {i + 1} Inc", "employer_ein": f"1{i}-000{i}111", "wages": f"{10_000 + 1_000 * i:,}.00", "federal_withheld": f"{600 + 50 * i:,}.00", "social_security_wages": f"{10_000 + 1_000 * i:,}.00", "social_security_tax": f"{(10_000 + 1_000 * i) * 0.062:,.2f}", "medicare_wages": f"{10_000 + 1_000 * i:,}.00", "medicare_tax": f"{(10_000 + 1_000 * i) * 0.0145:,.2f}", "control_number": f"B{i:04d}"}
        draw_w2(FIX / "batch_w2s" / f"w2_{i + 1:02d}.jpg", wi)
        (FIX / "batch_w2s" / f"w2_{i + 1:02d}.json").write_text(json.dumps({"kind": "W-2", "employee_name": wi["employee_name"], "employer_name": wi["employer_name"], "wages": 10_000 + 1_000 * i, "federal_withheld": 600 + 50 * i, "tax_year": "2025"}, indent=1))
    print("fixtures written to", FIX, "and demo images to", DEMO)


if __name__ == "__main__":
    main()
