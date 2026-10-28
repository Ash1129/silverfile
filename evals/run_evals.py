"""Run every evaluation case in evals/cases.json through the graph, offline.

    ./.venv/bin/python evals/run_evals.py            # all cases
    ./.venv/bin/python evals/run_evals.py --only clean_single two_w2s
    ./.venv/bin/python evals/run_evals.py --verbose  # show every mismatch
    ./.venv/bin/python evals/run_evals.py --json out.json

Each case either carries a confirmed intake (the conversation is skipped) or a
list of answers that are fed to the intake questions one per interrupt. When the
graph pauses on an escalation, the case's "resolution" table says what the
Kushim employee answers; an escalation with no entry ends the run there, so a
case can expect "waiting_human". Exit status is non-zero if any case fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Offline, in-process tax_math; set before silverfile is imported (its .env loader uses setdefault).
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("SILVERFILE_TAXMATH_DIRECT", "1")
os.environ.setdefault("SILVERFILE_ALLOW_FAULTS", "1")  # some cases inject faults (the hook is off outside tests and evals)
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from langgraph.types import Command  # noqa: E402

from silverfile.graph import compile_graph, get_checkpointer, new_return_state  # noqa: E402

MAX_STEPS = 200


# ---------------------------------------------------------------- case loading


def load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    return data["cases"] if isinstance(data, dict) else data


def resolve_intake(case: dict[str, Any]) -> dict[str, Any] | None:
    intake = case.get("intake")
    if intake is None:
        return None
    if isinstance(intake, str):
        intake = json.loads((ROOT / intake).read_text())
    intake = json.loads(json.dumps(intake))
    for k, v in (case.get("intake_patch") or {}).items():
        if k in ("taxpayer", "spouse") and isinstance(v, dict):
            intake[k] = {**(intake.get(k) or {}), **v}
        else:
            intake[k] = v
    return intake


def resolve_documents(case: dict[str, Any]) -> list[dict[str, Any]]:
    docs = []
    for d in case.get("documents") or []:
        path = ROOT / d["path"]
        if not path.exists():
            raise FileNotFoundError(f"{case['id']}: document {d['path']} not found")
        docs.append({"id": d["id"], "kind": d["kind"], "path": str(path), "declared_by": "eval", "uploaded_at": "2026-01-15T10:00:00+00:00"})
    return docs


# ---------------------------------------------------------------- driving the graph


def pending_interrupts(g, cfg) -> list[dict[str, Any]]:
    return [i.value for t in g.get_state(cfg).tasks for i in t.interrupts]


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    g = compile_graph(get_checkpointer(":memory:"))
    cfg = {"configurable": {"thread_id": f"eval-{case['id']}"}}
    intake = resolve_intake(case)
    documents = resolve_documents(case)
    answers = list(case.get("answers") or [])
    resolution = case.get("resolution") or {}
    conversational = intake is None

    g.invoke(new_return_state(cfg["configurable"]["thread_id"], [] if conversational else documents, intake=intake, fault_injection=case.get("fault_injection")), cfg)

    transcript: list[str] = []
    stopped_on: str | None = None
    for _ in range(MAX_STEPS):
        pending = pending_interrupts(g, cfg)
        if not pending:
            break
        if len(pending) != 1:
            raise RuntimeError(f"{case['id']}: {len(pending)} simultaneous interrupts")
        payload = pending[0]
        kind = payload.get("type")
        if kind == "escalation":
            esc_kind = payload["escalation"]["kind"]
            answer = resolution.get(esc_kind)
            if answer is None:
                stopped_on = esc_kind
                transcript.append(f"escalation {esc_kind}: no resolution given, stopping here")
                break
            transcript.append(f"escalation {esc_kind}: {answer.get('action')}")
            g.invoke(Command(resume={"resolved_by": "eval-runner", **answer}), cfg)
        elif kind in ("question", "upload"):
            if not answers:
                raise RuntimeError(f"{case['id']}: ran out of answers at question {payload.get('slot')!r}")
            text = answers.pop(0)
            transcript.append(f"{payload.get('slot')}: {text}")
            if kind == "upload":
                g.invoke(Command(resume={"text": text, "documents": documents}), cfg)
            else:
                g.invoke(Command(resume=text), cfg)
        else:
            raise RuntimeError(f"{case['id']}: unknown interrupt {payload!r}")
    else:
        raise RuntimeError(f"{case['id']}: did not finish within {MAX_STEPS} steps")

    state = g.get_state(cfg).values
    return {"state": state, "transcript": transcript, "stopped_on": stopped_on, "unused_answers": answers}


# ---------------------------------------------------------------- checking


def _lookup(state: dict[str, Any], dotted: str) -> Any:
    cur: Any = state
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def check_case(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    exp = case.get("expected") or {}
    state = result["state"]
    problems: list[str] = []
    if "status" in exp and state.get("status") != exp["status"]:
        problems.append(f"status: expected {exp['status']!r}, got {state.get('status')!r}")
    if "escalations" in exp:
        seen: list[str] = []
        for e in state.get("escalations", []):
            if e.get("kind") not in seen:
                seen.append(e.get("kind"))
        if seen != list(exp["escalations"]):
            problems.append(f"escalations: expected {exp['escalations']}, got {seen}")
    lines = state.get("lines") or {}
    for line, want in (exp.get("lines") or {}).items():
        got = lines.get(line)
        if got is None or abs(float(got) - float(want)) > 0.005:
            problems.append(f"line {line}: expected {want}, got {got}")
    for key, want in (exp.get("flags") or {}).items():
        got = _lookup(state, key)
        if got != want:
            problems.append(f"{key}: expected {want!r}, got {got!r}")
    if result["unused_answers"]:
        problems.append(f"{len(result['unused_answers'])} scripted answer(s) were never asked for: {result['unused_answers'][:3]}")
    if state.get("status") == "complete":
        if not state.get("review_passed"):
            problems.append("status complete but review_passed is not set")
        missing = [ln for ln, p in (state.get("provenance") or {}).items() if abs(float(p.get("amount", 0)) - float(lines.get(ln, 0))) > 0.005]
        if missing:
            problems.append(f"provenance disagrees with the draft on lines {missing}")
    return problems


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases.json"))
    ap.add_argument("--only", nargs="*", help="case ids to run")
    ap.add_argument("--verbose", "-v", action="store_true", help="show the transcript of every case")
    ap.add_argument("--json", help="write the results to this file")
    args = ap.parse_args(argv)

    cases = load_cases(Path(args.cases))
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]
        unknown = set(args.only) - {c["id"] for c in cases}
        if unknown:
            print(f"unknown case id(s): {sorted(unknown)}", file=sys.stderr)
            return 2

    rows: list[dict[str, Any]] = []
    t_all = time.perf_counter()
    for case in cases:
        t0 = time.perf_counter()
        try:
            result = run_case(case)
            problems = check_case(case, result)
            status = result["state"].get("status")
            kinds = [e.get("kind") for e in result["state"].get("escalations", [])]
            transcript = result["transcript"]
        except Exception as e:  # a crash is a failure with the exception as the problem
            problems, status, kinds, transcript = [f"{type(e).__name__}: {e}"], "error", [], []
        rows.append({"id": case["id"], "title": case.get("title", ""), "status": status, "escalations": kinds, "problems": problems, "seconds": round(time.perf_counter() - t0, 3), "transcript": transcript})

    width = max(len(r["id"]) for r in rows) if rows else 10
    print(f"{'case':<{width}}  {'result':<6}  {'status':<13}  {'escalations':<28}  secs")
    print("-" * (width + 62))
    for r in rows:
        verdict = "PASS" if not r["problems"] else "FAIL"
        esc = ",".join(r["escalations"]) or "-"
        print(f"{r['id']:<{width}}  {verdict:<6}  {str(r['status']):<13}  {esc:<28}  {r['seconds']:.2f}")
        for p in r["problems"]:
            print(f"{'':<{width}}    - {p}")
        if args.verbose and r["transcript"]:
            for line in r["transcript"]:
                print(f"{'':<{width}}    | {line}")
    passed = sum(1 for r in rows if not r["problems"])
    print("-" * (width + 62))
    print(f"{passed}/{len(rows)} cases passed in {time.perf_counter() - t_all:.2f}s")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1, default=str))
    return 0 if passed == len(rows) and rows else 1


if __name__ == "__main__":
    sys.exit(main())
