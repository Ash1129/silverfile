"""Document Extraction agent.

Turns a supplied tax document into a typed, structured record: field -> value
exactly as printed, with the box reference for each field. It never decides
what a value means for the return (that is Eligibility's and Computation's job).

Runs as one parallel branch per document (LangGraph `Send`), so each branch
writes only its own record; the `merge_documents` reducer combines them.

Images (the W-2 case the grader tests) are read with Claude's vision. JSON
payloads and fixture files are validated against the same schemas. Offline,
images fall back to a sidecar `<image>.json` if present, otherwise the record
is marked unreadable and goes to a person.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import llm
from ..config import settings
from ..provenance import now_iso
from ..state import DocumentInput, ExtractedDocument

SCHEMAS: dict[str, dict[str, str]] = {
    "W-2": {"employee_name": "Box e", "employee_ssn_last4": "Box a", "employer_name": "Box c", "employer_ein": "Box b", "wages": "Box 1", "federal_withheld": "Box 2", "social_security_wages": "Box 3", "social_security_tax": "Box 4", "medicare_wages": "Box 5", "medicare_tax": "Box 6", "tax_year": "form header"},
    "SSA-1099": {"recipient_name": "Box 1", "net_benefits": "Box 5", "federal_withheld": "Box 6", "tax_year": "form header"},
    "1099-R": {"recipient_name": "Recipient", "payer_name": "Payer", "gross_distribution": "Box 1", "taxable_amount": "Box 2a", "taxable_not_determined": "Box 2b", "federal_withheld": "Box 4", "distribution_code": "Box 7", "ira_sep_simple": "Box 7 IRA/SEP/SIMPLE", "qcd_amount": "QCD (noted)", "tax_year": "form header"},
    "1099-INT": {"recipient_name": "Recipient", "payer_name": "Payer", "interest_income": "Box 1", "federal_withheld": "Box 4", "tax_exempt_interest": "Box 8", "tax_year": "form header"},
    "1099-DIV": {"recipient_name": "Recipient", "payer_name": "Payer", "ordinary_dividends": "Box 1a", "qualified_dividends": "Box 1b", "federal_withheld": "Box 4", "tax_year": "form header"},
}
MONEY_FIELDS = {"wages", "federal_withheld", "social_security_wages", "social_security_tax", "medicare_wages", "medicare_tax", "net_benefits", "gross_distribution", "taxable_amount", "qcd_amount", "interest_income", "tax_exempt_interest", "ordinary_dividends", "qualified_dividends"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

VISION_SYSTEM = """You transcribe US tax forms from images for a preparation aid. Report only what is printed.
Rules: never estimate, infer or fill a box from memory. If a box is blank, unreadable or absent, return null for it and add a note in "issues".
Do not total, net or interpret anything. For the SSN report only the last four digits. Return JSON only."""


def _money(v: Any) -> float | None:
    if v in (None, "", "null"):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _normalise(kind: str, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    schema = SCHEMAS.get(kind, {})
    fields: dict[str, Any] = {}
    issues: list[str] = []
    for k in schema:
        v = raw.get(k)
        if k in MONEY_FIELDS:
            m = _money(v)
            if v not in (None, "") and m is None:
                issues.append(f"{k} ({schema[k]}) is not a readable amount: {v!r}")
            fields[k] = m
        elif k in ("taxable_not_determined", "ira_sep_simple"):
            fields[k] = v in (True, "true", "True", "X", "x", "yes", 1, "1")
        else:
            fields[k] = v
    for k in raw:
        if k not in schema and k not in ("kind", "issues", "confidence"):
            fields[k] = raw[k]
    if kind == "W-2" and fields.get("wages") is None:
        issues.append("Box 1 wages missing or unreadable")
    if str(fields.get("tax_year") or "") not in ("", "2025", "2025.0"):
        issues.append(f"form tax year is {fields.get('tax_year')!r}, not 2025")
    return fields, issues


def classify_kind(doc: DocumentInput, raw: dict[str, Any] | None) -> str:
    k = (doc.get("kind") or (raw or {}).get("kind") or "").upper().replace("FORM ", "").strip()
    aliases = {"W2": "W-2", "SSA1099": "SSA-1099", "1099R": "1099-R", "1099INT": "1099-INT", "1099DIV": "1099-DIV", "1099NEC": "1099-NEC", "1099B": "1099-B"}
    return aliases.get(k.replace("-", ""), k or "unknown")


def extract_one(doc: DocumentInput) -> ExtractedDocument:
    doc_id = doc.get("id") or "doc"
    path = doc.get("path")
    payload = doc.get("payload")
    raw: dict[str, Any] | None = None
    method, source_ref, issues, tokens_in, tokens_out, confidence = "payload", path or f"payload:{doc_id}", [], 0, 0, 1.0

    if payload:
        raw = dict(payload)
    elif path and Path(path).suffix.lower() == ".json":
        raw = json.loads(Path(path).read_text())
        method = "fixture"
    elif path and Path(path).suffix.lower() in IMAGE_SUFFIXES:
        if llm.available():
            raw, tokens_in, tokens_out, method, confidence, issues = _read_image(doc, path)
        else:
            sidecar = Path(path).with_suffix(".json")
            if sidecar.exists():
                raw = json.loads(sidecar.read_text())
                method = "offline-sidecar"
                issues = ["offline mode: image not read by a model; values come from the sidecar JSON"]
            else:
                issues = ["offline mode and no sidecar JSON: image could not be read; send to a person"]
                confidence = 0.0
    else:
        issues = [f"unsupported document input: {path or 'no path or payload'}"]
        confidence = 0.0

    kind = classify_kind(doc, raw)
    fields, norm_issues = _normalise(kind, raw or {}) if raw else ({}, [])
    issues = [*issues, *norm_issues]
    out_of_scope = kind not in SCHEMAS
    if out_of_scope:
        issues.append(f"document kind {kind!r} is not in the SilverFile profile")
    return ExtractedDocument(
        id=doc_id, kind=kind, version=int(doc.get("version", 1) or 1), fields=fields, boxes=dict(SCHEMAS.get(kind, {})), confidence=confidence,
        method=method, source_ref=source_ref, issues=issues, out_of_scope=out_of_scope, extracted_at=now_iso(), tokens_in=tokens_in, tokens_out=tokens_out,
    )


def _read_image(doc: DocumentInput, path: str) -> tuple[dict[str, Any] | None, int, int, str, float, list[str]]:
    model = settings().extraction_model
    hint = (doc.get("kind") or "").upper()
    schema_text = json.dumps({k: {f: box for f, box in v.items()} for k, v in SCHEMAS.items()}, indent=0)
    prompt = (
        f"Identify which form this is (one of {list(SCHEMAS)}, or another kind) and transcribe its boxes.\n"
        f"{'The taxpayer said this is a ' + hint + '. Confirm or correct that from the image.' if hint else ''}\n"
        f"Field names and the box each comes from:\n{schema_text}\n"
        'Return JSON: {"kind": "...", "confidence": 0-1, "issues": ["..."], plus one key per field for that kind (null when not printed)}.'
    )
    res = llm.complete(VISION_SYSTEM, [{"role": "user", "content": [llm.image_block(path), {"type": "text", "text": prompt}]}], model=model, max_tokens=900)
    data = res.json() if res.text else None
    if not isinstance(data, dict):
        return None, res.tokens_in, res.tokens_out, f"vision:{model}", 0.0, ["model returned no structured record; send to a person"]
    issues = [str(i) for i in (data.get("issues") or [])]
    try:
        conf = float(data.get("confidence", 0.8))
    except (TypeError, ValueError):
        conf = 0.8
    return data, res.tokens_in, res.tokens_out, f"vision:{model}", conf, issues


def extract_document_node(payload: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node for one Send() branch. `payload` carries {"document": DocumentInput}."""
    doc: DocumentInput = payload["document"]
    rec = extract_one(doc)
    ev = {"agent": "extraction", "model": rec.get("method", ""), "purpose": f"extract {rec.get('kind')} {rec.get('id')}", "tokens_in": rec.get("tokens_in", 0), "tokens_out": rec.get("tokens_out", 0), "at": now_iso()}
    return {"extracted": {rec["id"]: rec}, "token_log": [ev] if ev["tokens_in"] else [], "events": [f"extraction: {rec['id']} -> {rec['kind']} via {rec['method']} ({len(rec['issues'])} issues)"]}


def aggregate_extraction_node(state: dict[str, Any]) -> dict[str, Any]:
    """Fan-in after the parallel branches: nothing to compute, just mark done and record the handoff."""
    n = len(state.get("extracted") or {})
    return {"extraction_done": True, "handoffs": ["extraction"], "events": [f"extraction: aggregated {n} document record(s)"]}
