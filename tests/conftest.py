"""Shared test setup for SilverFile.

Every test runs offline: no API key (the agents use their scripted / sidecar
fallbacks) and tax_math called in-process. The MCP round-trip tests flip
SILVERFILE_TAXMATH_DIRECT back to "0" with monkeypatch for the few runs that
prove the stdio transport works.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

# Must happen before anything imports silverfile (config reads the environment at call time,
# but .env is loaded with setdefault at import, so our values must already be there).
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SILVERFILE_TAXMATH_DIRECT"] = "1"

from langgraph.types import Command  # noqa: E402

from silverfile.graph import compile_graph, get_checkpointer, new_return_state  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "data" / "fixtures"
DEMO = ROOT / "demo"
CASE1 = FIXTURES / "case1_clean"
CASE2 = FIXTURES / "case2_hitl"
CASE3 = FIXTURES / "case3_out_of_scope"
CASE4 = FIXTURES / "case4_r_box2b"


def document(doc_id: str, kind: str, path: Path | str) -> dict[str, Any]:
    return {"id": doc_id, "kind": kind, "path": str(path), "declared_by": "fixture", "uploaded_at": "2026-01-15T10:00:00+00:00"}


def case1_document_list() -> list[dict[str, Any]]:
    return [
        document("w2-1", "W-2", DEMO / "w2_image_1.jpg"),
        document("ssa-1", "SSA-1099", CASE1 / "ssa1099.json"),
        document("int-1", "1099-INT", CASE1 / "1099int.json"),
        document("r-1", "1099-R", CASE1 / "1099r.json"),
    ]


def case2_document_list() -> list[dict[str, Any]]:
    return [
        document("ssa-1", "SSA-1099", CASE2 / "ssa1099.json"),
        document("r-1", "1099-R", CASE2 / "1099r.json"),
        document("div-1", "1099-DIV", CASE2 / "1099div.json"),
    ]


def load_intake(case_dir: Path) -> dict[str, Any]:
    return json.loads((case_dir / "intake.json").read_text())


def interrupts(g, cfg) -> list[dict[str, Any]]:
    """The interrupt payloads the graph is waiting on; [] when the run has finished."""
    return [i.value for t in g.get_state(cfg).tasks for i in t.interrupts]


def new_graph():
    return compile_graph(get_checkpointer(":memory:"))


def run_prefilled(thread_id: str, documents: list[dict[str, Any]], intake: dict[str, Any], fault_injection: dict[str, Any] | None = None):
    """Run a return with a confirmed intake (skips the conversation). Returns (graph, cfg, final values)."""
    g = new_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    out = g.invoke(new_return_state(thread_id, documents, intake=intake, fault_injection=fault_injection), cfg)
    return g, cfg, out


def resolve(g, cfg, action: str, note: str = "test", facts_patch: dict[str, Any] | None = None):
    payload = {"action": action, "resolved_by": "test-reviewer", "note": note}
    if facts_patch:
        payload["facts_patch"] = facts_patch
    return g.invoke(Command(resume=payload), cfg)


@pytest.fixture
def case1_documents() -> list[dict[str, Any]]:
    return case1_document_list()


@pytest.fixture
def case2_documents() -> list[dict[str, Any]]:
    return case2_document_list()


@pytest.fixture
def case1_intake() -> dict[str, Any]:
    return load_intake(CASE1)


@pytest.fixture
def case2_intake() -> dict[str, Any]:
    return load_intake(CASE2)


@pytest.fixture
def case1_answers() -> list[str]:
    return json.loads((CASE1 / "answers.json").read_text())


@pytest.fixture
def graph():
    return new_graph()
