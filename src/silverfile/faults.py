"""Fault injection for tests: the mistakes the Review agent must catch.

Kept out of agents/computation.py so that module stays free of arithmetic
(tests/test_no_arithmetic.py checks it). Only active when the return state
carries a `fault_injection` dict, which the dashboard never sets.
"""
from __future__ import annotations

from typing import Any


def apply_computation_faults(fault: dict[str, Any], version: int, lines: dict[str, float], prov: dict[str, Any], notes: list[str]) -> None:
    if not fault:
        return
    rounds = int(fault.get("rounds", 1))
    if fault.get("drop_provenance_for") and version <= rounds:
        prov.pop(fault["drop_provenance_for"], None)
        notes.append(f"FAULT INJECTED (test): provenance for line {fault['drop_provenance_for']} dropped in version {version}")
    if fault.get("corrupt_line") and version <= rounds:
        line, delta = fault["corrupt_line"], float(fault.get("delta", 100))
        lines[line] = round(lines[line] + delta, 2)
        notes.append(f"FAULT INJECTED (test): line {line} shifted by {delta} in version {version}")
