"""The tax_math MCP stdio transport. These tests spawn the server as a
subprocess; everything else in the suite calls tax_math in-process."""
from __future__ import annotations

import pytest

from conftest import case1_document_list, load_intake, run_prefilled, CASE1
from silverfile.tax_math import rules
from silverfile.tax_math.client import TaxMathClient


@pytest.fixture
def mcp_env(monkeypatch):
    monkeypatch.setenv("SILVERFILE_TAXMATH_DIRECT", "0")


def test_client_lists_the_twelve_tools_and_calls_over_stdio(mcp_env):
    with TaxMathClient() as client:
        assert client.direct is False, "the MCP server could not be started; the client fell back to direct calls"
        tools = client.list_tools()
        assert len(tools) == 12
        assert tools == sorted(rules.TOOL_FUNCTIONS)
        res = client.call("income_tax", filing_status="single", taxable_income=22_898.01)
        assert res["tax"] == 2_509.26
        assert res["rule"] == "REF-TX-1"
        assert client.calls[-1].transport == "mcp-stdio"
        assert client.calls[-1].tool == "income_tax"
        # a second call on the same session, with a structured result
        sd = client.call("standard_deduction", filing_status="mfj", boxes_checked=3)
        assert sd["amount"] == 36_300.0
        assert all(c.transport == "mcp-stdio" for c in client.calls)


def test_spec_violation_travels_back_as_a_value_error(mcp_env):
    with TaxMathClient() as client:
        assert client.direct is False
        with pytest.raises(ValueError) as exc:
            client.call("taxable_1099r", gross=14_000.0, taxable_amount=None, taxable_not_determined=True)
        assert "box 2b" in str(exc.value)
        assert "out of scope" in str(exc.value)
        # the session is still usable afterwards
        assert client.call("settle", total_tax=2_509.26, total_payments=2_610.0) == {"overpaid": 100.74, "amount_owed": 0.0}


def test_unknown_tool_is_rejected_before_the_wire(mcp_env):
    with TaxMathClient() as client:
        with pytest.raises(KeyError):
            client.call("not_a_tool")
        assert client.calls == []


def test_direct_mode_reports_direct_transport():
    with TaxMathClient(direct=True) as client:
        client.call("sum_amounts", amounts=[1.0, 2.0])
        assert client.calls[-1].transport == "direct"
        assert client.list_tools() == sorted(rules.TOOL_FUNCTIONS)


def test_full_graph_run_over_mcp_marks_provenance_transport(mcp_env):
    _, _, out = run_prefilled("mcp-graph", case1_document_list(), load_intake(CASE1))
    assert out["status"] == "complete"
    assert out["provenance"]["16"]["transport"] == "mcp-stdio"
    assert out["provenance"]["16"]["method"] == "tax_math.income_tax"
    assert out["provenance"]["6b"]["transport"] == "mcp-stdio"
    assert out["lines"]["16"] == 2_509.26
    assert out["lines"]["6b"] == 13_205.84
    assert any("over ['mcp-stdio']" in e for e in out["events"] if e.startswith("computation v1"))
    # lines that are not tool results say so rather than claiming a transport
    assert out["provenance"]["25c"]["transport"] == "n/a"
