"""Fault injection for tests: the mistakes the Review agent must catch.

Kept out of agents/computation.py so that module stays free of arithmetic
(tests/test_no_arithmetic.py checks it). Only active when the return state
carries a `fault_injection` dict, which the dashboard never sets, and only
where SILVERFILE_ALLOW_FAULTS=1 (the tests and evals set it): in any other
environment the hook does nothing, and a checkpoint carrying one is refused.
"""
from __future__ import annotations

from typing import Any

from .security.validate import faults_allowed


def apply_computation_faults(fault: dict[str, Any], version: int, lines: dict[str, float], prov: dict[str, Any], notes: list[str]) -> None:
    if not fault or not faults_allowed():
        return
    rounds = int(fault.get("rounds", 1))
    if fault.get("drop_provenance_for") and version <= rounds:
        prov.pop(fault["drop_provenance_for"], None)
        notes.append(f"FAULT INJECTED (test): provenance for line {fault['drop_provenance_for']} dropped in version {version}")
    if fault.get("corrupt_line") and version <= rounds:
        line, delta = fault["corrupt_line"], float(fault.get("delta", 100))
        lines[line] = round(lines[line] + delta, 2)
        if fault.get("with_record"):  # the record moves too: only the independent recomputation can see it
            prov[line] = {**prov[line], "amount": lines[line]}
        notes.append(f"FAULT INJECTED (test): line {line} shifted by {delta} in version {version}")
