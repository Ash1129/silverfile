"""Helpers that build provenance records. Every dollar on the return has one."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .state import Provenance

LINE_LABELS: dict[str, str] = {
    "1z": "Total wages, salaries, tips",
    "2a": "Tax-exempt interest",
    "2b": "Taxable interest",
    "3a": "Qualified dividends",
    "3b": "Ordinary dividends",
    "4a": "IRA distributions (total)",
    "4b": "IRA distributions (taxable)",
    "5a": "Pensions and annuities (total)",
    "5b": "Pensions and annuities (taxable)",
    "6a": "Social Security benefits (total)",
    "6b": "Social Security benefits (taxable)",
    "7": "Capital gain or (loss)",
    "8": "Additional income (Schedule 1, line 10)",
    "9": "Total income",
    "10": "Adjustments to income (Schedule 1, line 26)",
    "11a": "Adjusted gross income",
    "12d": "Age / blindness boxes checked",
    "12e": "Standard deduction",
    "13b": "Additional deductions (Schedule 1-A)",
    "14": "Total deductions",
    "15": "Taxable income",
    "16": "Tax (rate schedule)",
    "17": "Amount from Schedule 2, line 3",
    "18": "Add lines 16 and 17",
    "19": "Child tax credit / credit for other dependents",
    "20": "Amount from Schedule 3, line 8",
    "21": "Add lines 19 and 20",
    "22": "Subtract line 21 from line 18",
    "23": "Other taxes (Schedule 2, line 21)",
    "24": "Total tax",
    "25a": "Federal income tax withheld from W-2",
    "25b": "Federal income tax withheld from 1099",
    "25c": "Federal income tax withheld, other forms",
    "25d": "Total federal income tax withheld",
    "26": "2025 estimated tax payments",
    "32": "Total other payments and refundable credits",
    "33": "Total payments",
    "34": "Overpaid",
    "37": "Amount you owe",
}

# Lines whose values exist on the form so a reviewer sees the whole picture.
ALL_LINES = list(LINE_LABELS)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record(line: str, amount: float, *, source_kind: str, sources: list[str], method: str, inputs: dict[str, Any] | None = None, transport: str = "n/a", citations: list[str] | None = None, entered_by: str | None = None, note: str | None = None) -> Provenance:
    return Provenance(
        line=line, label=LINE_LABELS.get(line, line), amount=round(float(amount), 2), source_kind=source_kind,  # type: ignore[arg-type]
        sources=list(sources), method=method, inputs=dict(inputs or {}), transport=transport, citations=list(citations or []),
        entered_by=entered_by, entered_at=now_iso(), note=note,
    )


def user_input(line: str, amount: float, entered_by: str, note: str | None = None) -> Provenance:
    """Last resort: a value the user typed, recorded as direct input with date and who entered it."""
    return record(line, amount, source_kind="user", sources=["intake"], method="direct user input", entered_by=entered_by, note=note)
