"""Deterministic, source-linked conflicts and evidence-backed transcription corrections.

Claims do not populate return lines. Corrections are authenticated human work
orders; only Extraction applies them, with the original upload left intact.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from .security import pii, validate

CLAIM_FIELDS = {
    "W-2": {"wages", "federal_withheld"},
    "SSA-1099": {"net_benefits", "federal_withheld"},
    "1099-R": {"gross_distribution", "taxable_amount", "federal_withheld", "qcd_amount"},
    "1099-INT": {"interest_income", "tax_exempt_interest", "federal_withheld"},
    "1099-DIV": {"ordinary_dividends", "qualified_dividends", "federal_withheld"},
}


def source_key(document: dict[str, Any]) -> str:
    """Bind a correction to one immutable upload or synthetic source payload."""
    source = {k: document.get(k) for k in ("sha256", "path", "payload", "kind")}
    return hashlib.sha256(json.dumps(source, sort_keys=True, default=str).encode()).hexdigest()


def active_documents(state: dict[str, Any]) -> dict[str, Any]:
    return {key: doc for key, doc in (state.get("extracted") or {}).items() if not doc.get("superseded_by")}


def clean_claims(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError("document_claims must be a list of at most 30 source-linked claims.")
    claims = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"document_id", "kind", "field", "value"}:
            raise ValueError("Each claim contains only document_id, kind, field and value.")
        kind, field = item.get("kind"), item.get("field")
        if not isinstance(kind, str) or not isinstance(field, str) or kind not in CLAIM_FIELDS or field not in CLAIM_FIELDS[kind]:
            raise ValueError("A claim must name a supported document kind and monetary field.")
        doc_id = item.get("document_id")
        if doc_id is not None and (not isinstance(doc_id, str) or not doc_id.strip() or len(doc_id) > 100 or pii.find_ssns(doc_id)):
            raise ValueError("document_id must identify an existing source without personal identifiers.")
        amount = validate.money(item.get("value"))
        if amount is None or not 0 <= amount <= validate.MAX_AMOUNT:
            raise ValueError("A claim needs a confirmed, nonnegative monetary value; unknown is not zero.")
        claims.append({"kind": kind, "field": field, "value": amount, **({"document_id": doc_id} if doc_id else {})})
    return claims


def evidence_conflicts(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare only explicitly linked claims and identical physical sources.

    Different documents from the same payer can be legitimate and are not
    assumed to be duplicates merely because their monetary values differ.
    """
    if not state.get("extraction_done"):
        return []
    docs = active_documents(state)
    conflicts: list[dict[str, Any]] = []

    def add(code: str, summary: str, sources: list[dict[str, Any]], **detail: Any) -> None:
        digest = hashlib.sha256(json.dumps([code, sources, detail], sort_keys=True).encode()).hexdigest()[:16]
        conflicts.append({"id": f"evidence:{digest}", "code": code, "summary": summary, "sources": sources,
                          "required_action": "Confirm the source and correct the claim or transcription from evidence, then recheck.", **detail})

    for index, claim in enumerate((state.get("intake") or {}).get("document_claims") or []):
        try:
            claim = clean_claims([claim])[0]
        except ValueError:
            add("invalid_claim", "A document claim is malformed and needs staff confirmation.", [{"type": "intake", "claim_index": index}])
            continue
        matches = [(key, doc) for key, doc in docs.items() if doc.get("kind") == claim["kind"] and (not claim.get("document_id") or key == claim["document_id"])]
        origin = {"type": "intake", "claim_index": index, **claim}
        if len(matches) != 1:
            add("claim_source", "A monetary claim needs exactly one identified source document.", [origin], candidate_documents=[key for key, _ in matches])
            continue
        key, doc = matches[0]
        amount = validate.money((doc.get("fields") or {}).get(claim["field"]))
        source = {"type": "document", "document_id": key, "version": doc.get("version"), "field": claim["field"],
                  "box": (doc.get("boxes") or {}).get(claim["field"]), "value": amount}
        if amount is None or abs(Decimal(str(amount)) - Decimal(str(claim["value"]))) > Decimal("0.005"):
            add("amount_conflict", f"The stated {claim['field']} does not match document {key}.", [origin, source], field=claim["field"])

    inputs = {doc["id"]: doc for doc in state.get("documents_in") or []}
    groups: dict[str, list[str]] = {}
    for key in docs:
        if key in inputs:
            document = inputs[key]
            # A digest of bytes is stronger than the storage path (uploads use random paths).
            identity = document.get("sha256") or document.get("path")
            if identity:
                groups.setdefault(str(identity), []).append(key)
    for ids in groups.values():
        if len(ids) > 1:
            sources = [{"type": "document", "document_id": key, "version": docs[key].get("version"), "fields": docs[key].get("fields")} for key in ids]
            add("duplicate_source", "The same source was supplied more than once; counting it twice could duplicate income or withholding.", sources)
    return conflicts


def clean_document_patch(value: Any, state: dict[str, Any]) -> dict[str, Any]:
    from .agents.extraction import CHECKBOX_FIELDS, MONEY_FIELDS, SCHEMAS

    if not isinstance(value, dict) or set(value) - {"document_id", "expected_version", "fields", "evidence", "superseded_by"}:
        raise ValueError("A document correction contains document_id, expected_version, fields, evidence and optional superseded_by.")
    key = value.get("document_id")
    doc = (state.get("extracted") or {}).get(key) if isinstance(key, str) else None
    inputs = {d["id"]: d for d in state.get("documents_in") or []}
    if not doc or key not in inputs or doc.get("kind") not in SCHEMAS or doc.get("superseded_by"):
        raise ValueError("The correction must identify an active, supported document on this return.")
    if type(value.get("expected_version")) is not int or value["expected_version"] != doc.get("version"):
        raise ValueError("The document version changed. Reload this return before correcting it.")
    if doc.get("security_flags"):
        raise ValueError("A security-flagged document requires a preparer handoff; field correction cannot clear security findings.")
    evidence = value.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000:
        raise ValueError("A document correction needs an evidence explanation of 1 to 4,000 characters referencing the original source and boxes.")
    fields = value.get("fields", {})
    schema = SCHEMAS[doc["kind"]]
    if not isinstance(fields, dict) or set(fields) - set(schema):
        raise ValueError("Only fields in this document's displayed box schema can be corrected.")
    if not fields and not value.get("superseded_by"):
        raise ValueError("A correction needs changed fields or a confirmed identical-source duplicate.")
    normalised, issues, flags = validate.clean_fields(doc["kind"], {**doc.get("fields", {}), **fields}, schema, MONEY_FIELDS, CHECKBOX_FIELDS)
    if flags or any(normalised.get(field) is None for field in fields):
        raise ValueError("Corrected fields must contain readable, valid values without security flags.")
    duplicate = value.get("superseded_by")
    if duplicate is not None:
        other = active_documents(state).get(duplicate) if isinstance(duplicate, str) else None
        other_input = inputs.get(duplicate) if isinstance(duplicate, str) else None
        original = inputs[key]
        same_source = other_input and ((original.get("sha256") and original.get("sha256") == other_input.get("sha256")) or (original.get("path") and original.get("path") == other_input.get("path") and source_key(original) == source_key(other_input)))
        if duplicate == key or not other or other.get("kind") != doc["kind"] or not same_source or other.get("security_flags"):
            raise ValueError("Only another active copy of the identical source can supersede this document.")
        if fields:
            raise ValueError("Duplicate disposition and transcription changes are separate actions.")
    return {"document_id": key, "expected_version": value["expected_version"], "fields": {k: normalised[k] for k in fields},
            "evidence": pii.redact(evidence.strip()), **({"superseded_by": duplicate} if duplicate else {})}


def corrected_record(document: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Extraction's deterministic application of an authenticated work order."""
    from .agents.extraction import _normalise
    from .provenance import now_iso

    if override.get("source_key") != source_key(document):
        raise ValueError("The correction does not belong to this source document.")
    fields, issues, flags = _normalise(override["original_record"]["kind"], override["fields"])
    return {**override["original_record"], "fields": fields, "issues": issues, "security_flags": flags,
            "method": "staff-transcription", "version": document["version"], "extracted_at": now_iso(),
            "correction": {k: override[k] for k in ("actor", "at", "evidence", "previous_version")},
            "superseded_by": override.get("superseded_by"), "tokens_in": 0, "tokens_out": 0}
