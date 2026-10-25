"""The reducers that make the extraction fan-out safe, and the fan-out itself."""
from __future__ import annotations

import copy

from conftest import case1_document_list, load_intake, run_prefilled, CASE1
from silverfile.state import merge_documents, merge_escalations


def _rec(doc_id: str, version: int = 1, wages: float = 100.0, issues=None):
    return {"id": doc_id, "kind": "W-2", "version": version, "fields": {"wages": wages}, "issues": list(issues or []), "method": "fixture"}


# ---------------------------------------------------------------- merge_documents


def test_merge_documents_is_order_independent():
    a = {"a": _rec("a")}
    b = {"b": _rec("b")}
    assert merge_documents(a, b) == merge_documents(b, a) == {"a": _rec("a"), "b": _rec("b")}


def test_merge_documents_handles_none_sides():
    assert merge_documents(None, None) == {}
    assert merge_documents({"a": _rec("a")}, None) == {"a": _rec("a")}
    assert merge_documents(None, {"a": _rec("a")}) == {"a": _rec("a")}


def test_merge_documents_higher_version_wins_either_way():
    v1, v2 = _rec("a", 1, wages=100.0), _rec("a", 2, wages=200.0)
    assert merge_documents({"a": v1}, {"a": v2})["a"] == v2
    assert merge_documents({"a": v2}, {"a": v1})["a"] == v2


def test_merge_documents_equal_versions_keep_later_and_flag_the_duplicate():
    first = _rec("a", 1, wages=100.0, issues=["first issue"])
    second = _rec("a", 1, wages=150.0, issues=["second issue"])
    out = merge_documents({"a": first}, {"a": second})
    assert out["a"]["fields"]["wages"] == 150.0
    assert out["a"]["issues"] == ["first issue", "second issue", "duplicate write for the same document version was merged"]
    # the originals are not mutated
    assert first["issues"] == ["first issue"] and second["issues"] == ["second issue"]


def test_merge_documents_does_not_flag_the_same_object_twice():
    rec = _rec("a")
    assert merge_documents({"a": rec}, {"a": rec})["a"] is rec


def test_merge_documents_does_not_mutate_inputs():
    left = {"a": _rec("a")}
    snapshot = copy.deepcopy(left)
    merge_documents(left, {"b": _rec("b")})
    assert left == snapshot


# ---------------------------------------------------------------- merge_escalations


def _esc(esc_id: str, status: str = "open", kind: str = "conflict"):
    return {"id": esc_id, "kind": kind, "status": status, "reason": esc_id}


def test_merge_escalations_replaces_by_id_in_place():
    left = [_esc("e1"), _esc("e2")]
    out = merge_escalations(left, [_esc("e1", "resolved")])
    assert [e["id"] for e in out] == ["e1", "e2"]
    assert out[0]["status"] == "resolved" and out[1]["status"] == "open"


def test_merge_escalations_appends_new_ids_in_order():
    out = merge_escalations([_esc("e1")], [_esc("e3"), _esc("e2")])
    assert [e["id"] for e in out] == ["e1", "e3", "e2"]


def test_merge_escalations_handles_none_and_duplicates_within_right():
    assert merge_escalations(None, None) == []
    out = merge_escalations(None, [_esc("e1"), _esc("e1", "resolved")])
    assert len(out) == 1 and out[0]["status"] == "resolved"


def test_merge_escalations_does_not_mutate_left():
    left = [_esc("e1")]
    merge_escalations(left, [_esc("e1", "resolved")])
    assert left[0]["status"] == "open"


# ---------------------------------------------------------------- the fan-out through the graph


def _strip_times(extracted):
    return {k: {kk: vv for kk, vv in v.items() if kk != "extracted_at"} for k, v in extracted.items()}


def test_graph_extracts_all_four_documents_regardless_of_order():
    intake = load_intake(CASE1)
    docs = case1_document_list()
    _, _, forward = run_prefilled("order-a", docs, intake)
    _, _, backward = run_prefilled("order-b", list(reversed(docs)), intake)
    assert set(forward["extracted"]) == {"w2-1", "ssa-1", "int-1", "r-1"}
    assert _strip_times(forward["extracted"]) == _strip_times(backward["extracted"])
    assert forward["lines"] == backward["lines"]
    assert forward["status"] == backward["status"] == "complete"


def test_fan_out_records_one_extraction_handoff_and_one_event_per_document():
    _, _, out = run_prefilled("fan-in", case1_document_list(), load_intake(CASE1))
    assert out["handoffs"].count("extraction") == 1  # the aggregation step is the handoff, not each branch
    per_doc = [e for e in out["events"] if e.startswith("extraction: ") and " -> " in e]
    assert len(per_doc) == 4
    assert {e.split()[1] for e in per_doc} == {"w2-1", "ssa-1", "int-1", "r-1"}
    assert any(e.startswith("extraction: aggregated 4 document") for e in out["events"])
    assert out["extraction_done"] is True


def test_every_extracted_record_carries_its_version_and_method():
    _, _, out = run_prefilled("versions", case1_document_list(), load_intake(CASE1))
    for rec in out["extracted"].values():
        assert rec["version"] == 1
    assert out["extracted"]["w2-1"]["method"] == "offline-sidecar"
    assert out["extracted"]["ssa-1"]["method"] == "fixture"
