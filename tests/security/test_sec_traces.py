"""PII-free traces: nothing an operator, a log reader or a grader sees holds a
full SSN, and the stores on disk hold no PII in the clear.

1. The traces, docs and demo material committed to the repo hold no full SSN.
2. A run of the demo returns through the service layer: the trace each return
   carries (events, transcript, state.json), the index, the security log and
   every log line have no full SSN.
3. The raw database files and uploads contain neither the SSNs nor the
   taxpayers' names in the clear (encryption at rest).
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT
from silverfile import seed, service
from silverfile.security import audit, auth, pii

TRACE_DIRS = ("traces",)  # outputs: no full SSN at all, test series or not
INPUT_DIRS = ("demo", "evals", "data", "docs")  # scripts and walkthroughs: what a person types in a demo
# Public demonstration numbers a walkthrough may tell someone to type to show the rejection
# (123-45-6789 is the classic example; 078-05-1120 the voided Woolworth card). Never a real person's.
DEMO_REJECTION_EXAMPLES = {"123-45-6789", "078-05-1120"}
TEXT_SUFFIXES = {".json", ".log", ".md", ".html", ".txt", ".csv", ".mmd", ".jsonl"}


def _tracked(*dirs: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", *dirs], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    return [ROOT / p for p in out if Path(p).suffix in TEXT_SUFFIXES]


def test_committed_traces_and_docs_hold_no_full_ssn():
    files = _tracked(*TRACE_DIRS)
    assert any("traces/" in str(f) for f in files)
    leaks = {str(f.relative_to(ROOT)): pii.find_ssns(f.read_text(errors="ignore"))[:3] for f in files if pii.find_ssns(f.read_text(errors="ignore"))}
    assert leaks == {}, f"full SSNs in committed files: {leaks}"


def test_committed_inputs_hold_only_test_ssns():
    files = _tracked(*INPUT_DIRS)
    real = {str(f.relative_to(ROOT)): [s for s in pii.find_ssns(f.read_text(errors="ignore")) if not pii.is_test_ssn(s) and s not in DEMO_REJECTION_EXAMPLES][:3] for f in files}
    assert {k: v for k, v in real.items() if v} == {}
    # and the demonstration numbers are ones the system refuses
    assert not any(pii.validate_test_ssn(s)[0] for s in DEMO_REJECTION_EXAMPLES)


@pytest.fixture
def seeded(data_dir, caplog):
    pii.install_log_redaction()
    with caplog.at_level(logging.DEBUG):
        ids = seed.seed_demo_returns()
    return ids, caplog


def _known_ssns() -> set[str]:
    out = set()
    for case in (ROOT / "data" / "fixtures").glob("case*/intake.json"):
        facts = json.loads(case.read_text())
        for person in [facts.get("taxpayer") or {}, facts.get("spouse") or {}, *(facts.get("dependents") or [])]:
            if person.get("ssn"):
                out.add(person["ssn"])
    return out


def test_runtime_traces_and_logs_hold_no_full_ssn(seeded):
    ids, caplog = seeded
    assert ids and _known_ssns()
    for rid in ids:
        trace = json.dumps({"events": service.get_state(rid).get("events"), "transcript": service.transcript(rid), "state": service.state_json(rid)}, default=str)
        assert not pii.find_ssns(trace), f"{rid}: {pii.find_ssns(trace)[:2]}"
    assert not pii.find_ssns(json.dumps(service.list_returns()))
    assert not pii.find_ssns(json.dumps(audit.recent(1000)))
    assert not pii.find_ssns(caplog.text)


def test_stores_on_disk_hold_no_pii_in_the_clear(seeded, data_dir):
    ids, _ = seeded
    rid = ids[0]
    jpeg = (ROOT / "demo" / "w2_image_1.jpg").read_bytes()
    doc = service.save_upload(rid, "w2.jpg", jpeg, actor=auth.system("test"))
    names = [n.encode() for n in ("Whitfield", "Benson", "Okafor", "Eleanor")]
    secrets = [s.encode() for s in _known_ssns()] + [s.replace("-", "").encode() for s in _known_ssns()]
    files = [p for p in Path(data_dir).rglob("*") if p.is_file()]
    assert any(p.name == "checkpoints.sqlite" for p in files) and any(p.suffix == ".enc" for p in files)
    for p in files:
        raw = p.read_bytes()
        for needle in secrets + names:
            assert needle not in raw, f"{needle!r} in the clear in {p.relative_to(data_dir)}"
    assert Path(doc["path"]).read_bytes()[:3] != b"\xff\xd8\xff"  # the upload is not a readable JPEG on disk
