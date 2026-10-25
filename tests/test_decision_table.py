"""Table-driven checks of the four rules in the brief against tax_math.

Each row is (inputs, expected). Numbers are tax-year-2025 constants from
data/reference (REF-EL-1, REF-SD-1/2, REF-SR-1, REF-SS-1, REF-TX-1, REF-RT-1).
"""
from __future__ import annotations

import pytest

from silverfile.tax_math import rules as tm

# ---------------------------------------------------------------- 1040-SR eligibility (REF-EL-1)


@pytest.mark.parametrize(
    "taxpayer_dob, spouse_dob, filing_status, expected",
    [
        ("1961-01-01", None, "single", True),  # born before 2 January 1961: the last eligible day
        ("1961-01-02", None, "single", False),  # one day later: not eligible
        ("1960-12-31", None, "single", True),
        ("1970-06-15", None, "single", False),
        ("1970-06-15", "1955-01-01", "mfj", True),  # joint return: the spouse alone qualifies
        ("1970-06-15", "1955-01-01", "mfs", False),  # separate return: the spouse does not count
        ("1970-06-15", "1955-01-01", "single", False),
        ("1970-06-15", "1961-01-02", "mfj", False),  # neither qualifies
    ],
)
def test_1040sr_eligibility(taxpayer_dob, spouse_dob, filing_status, expected):
    res = tm.is_1040sr_eligible(taxpayer_dob, spouse_dob, filing_status)
    assert res["eligible"] is expected
    assert res["rule"] == "REF-EL-1"


def test_eligibility_reports_who_met_the_age_test():
    res = tm.is_1040sr_eligible("1970-06-15", "1955-01-01", "mfj")
    assert res["taxpayer_meets_age"] is False and res["spouse_meets_age"] is True


def test_eligibility_rejects_unknown_filing_status():
    with pytest.raises(ValueError):
        tm.is_1040sr_eligible("1950-01-01", None, "widow")


# ---------------------------------------------------------------- line 12d boxes (REF-SD-3)


@pytest.mark.parametrize(
    "tp_dob, tp_blind, sp_dob, sp_blind, fs, count",
    [
        ("1954-03-14", False, None, False, "single", 1),
        ("1954-03-14", True, None, False, "single", 2),
        ("1949-11-02", True, "1951-06-21", False, "mfj", 3),
        ("1949-11-02", True, "1951-06-21", True, "mfj", 4),
        ("1949-11-02", False, "1951-06-21", True, "mfs", 1),  # spouse boxes do not count on MFS
        ("1965-01-01", False, None, False, "hoh", 0),
    ],
)
def test_count_12d_boxes(tp_dob, tp_blind, sp_dob, sp_blind, fs, count):
    assert tm.count_12d_boxes(tp_dob, tp_blind, sp_dob, sp_blind, fs)["count"] == count


# ---------------------------------------------------------------- standard deduction (REF-SD-1 / REF-SD-2)

BASE = {"single": 15_750.0, "mfs": 15_750.0, "mfj": 31_500.0, "qss": 31_500.0, "hoh": 23_625.0}
PER_BOX = {"single": 2_000.0, "hoh": 2_000.0, "mfj": 1_600.0, "mfs": 1_600.0, "qss": 1_600.0}


@pytest.mark.parametrize("filing_status", list(BASE))
@pytest.mark.parametrize("boxes", [0, 1, 2, 3, 4])
def test_standard_deduction_chart(filing_status, boxes):
    res = tm.standard_deduction(filing_status, boxes)
    assert res["amount"] == BASE[filing_status] + PER_BOX[filing_status] * boxes
    assert res["per_box"] == PER_BOX[filing_status]
    assert res["base"] == BASE[filing_status]


@pytest.mark.parametrize(
    "filing_status, boxes, expected",
    [
        ("single", 1, 17_750.0),  # the case1 number
        ("single", 2, 19_750.0),
        ("hoh", 1, 25_625.0),
        ("mfj", 3, 36_300.0),  # the case2 number after the MFJ decision
        ("mfj", 2, 34_700.0),
        ("mfs", 1, 17_350.0),
        ("qss", 1, 33_100.0),
    ],
)
def test_standard_deduction_spot_values(filing_status, boxes, expected):
    assert tm.standard_deduction(filing_status, boxes)["amount"] == expected


def test_standard_deduction_chart_not_applicable_raises():
    with pytest.raises(ValueError, match="does not apply"):
        tm.standard_deduction("single", 1, chart_applies=False)


@pytest.mark.parametrize("boxes", [-1, 5])
def test_standard_deduction_rejects_bad_box_count(boxes):
    with pytest.raises(ValueError):
        tm.standard_deduction("single", boxes)


# ---------------------------------------------------------------- senior deduction (REF-SR-1)


@pytest.mark.parametrize(
    "filing_status, magi, taxpayer_65, spouse_65, expected",
    [
        ("single", 75_000.0, True, False, 6_000.0),  # at the threshold: no phase-out
        ("single", 85_000.0, True, False, 5_400.0),  # 10,000 over at 6% = 600 off
        ("single", 175_000.0, True, False, 0.0),  # fully phased out
        ("single", 46_648.01, True, False, 6_000.0),  # case1
        ("mfj", 150_000.0, True, True, 12_000.0),  # both 65 at the joint threshold
        ("mfj", 160_000.0, True, True, 10_800.0),  # 600 off each
        ("mfj", 160_000.0, True, False, 5_400.0),  # one qualifying spouse
        ("single", 50_000.0, False, False, 0.0),  # a 64-year-old gets nothing
        ("mfs", 80_000.0, True, True, 5_700.0),  # spouse never counts outside MFJ
        ("hoh", 76_000.0, True, False, 5_940.0),
    ],
)
def test_senior_deduction(filing_status, magi, taxpayer_65, spouse_65, expected):
    res = tm.senior_deduction(filing_status, magi, taxpayer_65, spouse_65)
    assert res["amount"] == expected
    assert res["rule"] == "REF-SR-1"


def test_senior_deduction_details():
    res = tm.senior_deduction("mfj", 160_000.0, True, True)
    assert res["qualifying_persons"] == 2
    assert res["threshold"] == 150_000.0
    assert res["excess_magi"] == 10_000.0
    assert res["reduction_per_person"] == 600.0


# ---------------------------------------------------------------- taxable Social Security (REF-SS-1)


@pytest.mark.parametrize(
    "filing_status, benefits, agi_ex, tax_exempt, expected, tier",
    [
        ("single", 10_000.0, 15_000.0, 0.0, 0.0, "below-base"),  # PI 20,000 <= 25,000
        ("single", 10_000.0, 25_000.0, 0.0, 2_500.0, "first-tier"),  # PI 30,000: half the excess over base
        ("single", 10_000.0, 20_000.0, 5_000.0, 2_500.0, "first-tier"),  # tax-exempt interest counts toward PI
        ("single", 20_000.0, 30_000.0, 0.0, 9_600.0, "second-tier"),  # 0.85 * 6,000 + 4,500 cap
        ("mfj", 20_000.0, 40_000.0, 0.0, 11_100.0, "second-tier"),  # 0.85 * 6,000 + 6,000 cap
        ("single", 20_000.0, 60_000.0, 0.0, 17_000.0, "second-tier"),  # capped at 85% of benefits
        ("single", 0.0, 50_000.0, 0.0, 0.0, "none"),  # no benefits: nothing taxable
        ("single", 21_600.0, 33_442.17, 0.0, 13_205.84, "second-tier"),  # the exact case1 worksheet
        ("mfj", 26_400.0, 32_520.55, 0.0, 7_462.47, "second-tier"),  # the case2 worksheet after MFJ
    ],
)
def test_taxable_social_security(filing_status, benefits, agi_ex, tax_exempt, expected, tier):
    res = tm.taxable_social_security(filing_status, benefits, agi_ex, tax_exempt)
    assert res["taxable"] == expected
    assert res["tier"] == tier


def test_provisional_income_is_half_benefits_plus_other_income():
    assert tm.provisional_income(33_442.17, 0.0, 21_600.0) == 44_242.17


def test_mfs_lived_with_spouse_taxes_85_percent():
    assert tm.taxable_social_security("mfs", 10_000.0, 0.0, 0.0, lived_with_spouse_mfs=True)["taxable"] == 8_500.0


# ---------------------------------------------------------------- income tax (REF-TX-1)


@pytest.mark.parametrize(
    "filing_status, taxable_income, expected",
    [
        ("single", 0.0, 0.0),
        ("single", 11_925.0, 1_192.50),  # top of the 10% bracket
        ("single", 11_926.0, 1_192.62),  # first dollar at 12%
        ("single", 22_898.01, 2_509.26),  # case1
        ("single", 48_475.0, 5_578.50),  # top of the 12% bracket
        ("single", 48_476.0, 5_578.72),  # first dollar at 22%
        ("single", 103_350.0, 17_651.00),
        ("mfj", 23_850.0, 2_385.00),
        ("mfj", 50_000.0, 5_523.00),
        ("mfj", 96_950.0, 11_157.00),
        ("hoh", 17_000.0, 1_700.00),
        ("mfs", 11_925.0, 1_192.50),
    ],
)
def test_income_tax_at_bracket_edges(filing_status, taxable_income, expected):
    assert tm.income_tax(filing_status, taxable_income)["tax"] == expected


def test_income_tax_breaks_out_brackets():
    res = tm.income_tax("single", 22_898.01)
    assert [r["rate"] for r in res["brackets"]] == [0.10, 0.12]
    assert res["brackets"][0]["tax"] == 1_192.50
    assert res["brackets"][1]["from"] == 11_925.0


def test_income_tax_negative_income_is_zero():
    assert tm.income_tax("single", -500.0)["tax"] == 0.0


# ---------------------------------------------------------------- 1099-R taxable portion (REF-RT-1)


def test_1099r_box_2b_checked_raises():
    with pytest.raises(ValueError, match="box 2b"):
        tm.taxable_1099r(14_000.0, None, True)


def test_1099r_taxable_defaults_to_gross():
    assert tm.taxable_1099r(14_200.0, None, False)["taxable"] == 14_200.0


def test_1099r_taxable_amount_is_used_when_printed():
    assert tm.taxable_1099r(14_200.0, 12_000.0, False)["taxable"] == 12_000.0


def test_1099r_qcd_reduces_taxable_for_an_ira():
    res = tm.taxable_1099r(10_000.0, 10_000.0, False, qcd_amount=3_000.0, is_ira=True)
    assert res["taxable"] == 7_000.0 and res["gross"] == 10_000.0 and res["qcd"] == 3_000.0


def test_1099r_qcd_ignored_for_a_pension():
    assert tm.taxable_1099r(10_000.0, 10_000.0, False, qcd_amount=3_000.0, is_ira=False)["taxable"] == 10_000.0


def test_1099r_taxable_above_gross_raises():
    with pytest.raises(ValueError, match="exceeds gross"):
        tm.taxable_1099r(10_000.0, 12_000.0, False)


# ---------------------------------------------------------------- settle and the helpers


@pytest.mark.parametrize(
    "total_tax, payments, overpaid, owed",
    [
        (2_509.26, 2_610.0, 100.74, 0.0),  # case1
        (3_877.76, 2_920.0, 0.0, 957.76),  # the two-W-2 case
        (0.0, 4_000.0, 4_000.0, 0.0),  # case2 after MFJ
        (100.0, 100.0, 0.0, 0.0),
    ],
)
def test_settle(total_tax, payments, overpaid, owed):
    assert tm.settle(total_tax, payments) == {"overpaid": overpaid, "amount_owed": owed}


def test_helpers_round_to_cents():
    assert tm.sum_amounts([18_400.0, 842.17, 14_200.0, 13_205.84]) == 46_648.01
    assert tm.subtract(46_648.01, 0.0) == 46_648.01
    assert tm.nonnegative_difference(10.0, 20.0) == 0.0
    assert tm.nonnegative_difference(46_648.01, 23_750.0) == 22_898.01


def test_every_tool_is_registered():
    assert set(tm.TOOL_FUNCTIONS) == {
        "sum_amounts", "subtract", "nonnegative_difference", "is_1040sr_eligible", "count_12d_boxes", "standard_deduction",
        "senior_deduction", "provisional_income", "taxable_social_security", "taxable_1099r", "income_tax", "settle",
    }
