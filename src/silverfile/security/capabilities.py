"""Least privilege for tools: each agent gets only the tax_math functions it uses.

No model in SilverFile is given a tool; the models return text that code
parses. The deterministic agents call tax_math, and each gets a view of it that
holds only its own functions (and the constants it reads). Asking for anything
else raises before the call is made. tests/security/test_sec_structure.py
checks these sets against the code, and checks that no agent imports a module
outside its lane.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ..tax_math import rules

AGENT_TOOLS: dict[str, frozenset[str]] = {
    # Computation builds every line; it needs the arithmetic and the worksheets, not the eligibility tests
    "computation": frozenset({"sum_amounts", "subtract", "nonnegative_difference", "standard_deduction", "senior_deduction", "taxable_social_security", "taxable_1099r", "income_tax", "settle"}),
    # Eligibility decides; it never computes a dollar amount
    "eligibility": frozenset({"is_1040sr_eligible", "count_12d_boxes"}),
    # Review recomputes the derived lines independently, so it needs the same arithmetic as Computation, minus 1099-R parsing
    "review": frozenset({"sum_amounts", "subtract", "nonnegative_difference", "standard_deduction", "senior_deduction", "taxable_social_security", "income_tax", "settle"}),
}
AGENT_CONSTANTS: dict[str, frozenset[str]] = {"eligibility": frozenset({"FILING_STATUSES"})}
# Intake, Extraction, the Orchestrator and Escalation get no tax_math at all.


class ToolNotAllowed(PermissionError):
    pass


def allowed(agent: str, tool: str) -> bool:
    return tool in AGENT_TOOLS.get(agent, frozenset())


def check(agent: str, tool: str) -> None:
    if not allowed(agent, tool):
        raise ToolNotAllowed(f"the {agent} agent may not call tax_math.{tool}")


def tools_for(agent: str) -> Any:
    """A namespace holding only this agent's functions and constants."""
    names = AGENT_TOOLS.get(agent, frozenset()) | AGENT_CONSTANTS.get(agent, frozenset())
    return SimpleNamespace(**{n: (rules.TOOL_FUNCTIONS[n] if n in rules.TOOL_FUNCTIONS else getattr(rules, n)) for n in sorted(names)})
