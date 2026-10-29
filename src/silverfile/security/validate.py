"""Schema validation for values that cross a trust boundary.

- clean_fields: what Document Extraction read (from a model, a sidecar or a
  payload) becomes a record of known fields, each of the right type and in a
  sane range. Unknown fields are dropped, text is cleaned and capped, a full
  SSN in any text field is removed, and instruction-like text is flagged.
- clean_facts_patch: the facts a Kushim employee corrects when resolving an
  escalation (only known keys, typed, test SSNs only).
- check_state: a return's state as loaded from a checkpoint matches the shapes
  the agents write, so a replayed or edited checkpoint is refused.
"""
from __future__ import annotations

import os
import re
from typing import Any

from . import injection, pii

MAX_AMOUNT = 10_000_000.0  # no in-scope 1099 or W-2 box legitimately exceeds this for this profile
MAX_TEXT = 120
CHECKBOX_TRUE = (True, "true", "True", "X", "x", "yes", "Yes", 1, "1")
CHECKBOX_FALSE = (False, "false", "False", "no", "No", 0, "0", "unchecked")
FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Extra fields a known form may carry beyond its box schema, because an agent reads them.
EXTRA_FIELDS = {"1099-R": {"account_type"}}
FORMATS = {
    "employer_ein": re.compile(r"^\d{2}-?\d{7}$"),
    "tax_year": re.compile(r"^\d{4}$"),
    "distribution_code": re.compile(r"^[0-9A-Z]{1,2}$"),
    "account_type": re.compile(r"^[A-Za-z /-]{1,20}$"),
}


def _text(v: Any, field: str, issues: list[str], flags: list[str]) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(int(v)) if float(v).is_integer() else str(v)
    if not isinstance(v, str):
        issues.append(f"{field}: expected text, got {type(v).__name__}; dropped")
        return None
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", v).strip()
    if pii.find_ssns(s):
        flags.append(f"{field}: a full Social Security number appeared in a text field and was removed")
        s = pii.remove_ssns(s, "[SSN removed]")
    if len(s) > MAX_TEXT:
        issues.append(f"{field}: text longer than {MAX_TEXT} characters was cut")
        s = s[:MAX_TEXT]
    return s or None


def money(v: Any) -> float | None:
    if v in (None, "", "null"):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def checkbox(v: Any) -> bool | None:
    """Three-valued input: an unrecognised or missing checkbox is unknown."""
    if v in CHECKBOX_TRUE:
        return True
    if v in CHECKBOX_FALSE:
        return False
    return None


def unknown_fact_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic requirements for facts that must not silently become zero/false."""
    facts = state.get("intake") or {}
    unknown = set(facts.get("_unknown_fields") or [])
    for key in ("estimated_payments", "schedule1_adjustments", "schedule1_other_income", "schedule2_line3", "schedule2_line21", "schedule3_line8", "can_be_claimed_as_dependent"):
        if key in facts and facts[key] is None:
            unknown.add(key)
    for doc_id, doc in (state.get("extracted") or {}).items():
        fields = doc.get("fields") or {}
        if doc.get("kind") == "1099-R":
            for key in ("taxable_not_determined", "ira_sep_simple"):
                if fields.get(key) is None:
                    unknown.add(f"document:{doc_id}:{key}")
        for key, value in fields.items():
            if value is None and key in {"wages", "net_benefits", "gross_distribution", "taxable_amount", "qcd_amount", "interest_income", "tax_exempt_interest", "ordinary_dividends", "qualified_dividends", "federal_withheld"}:
                if key != "taxable_amount" or fields.get("taxable_not_determined") is not True:
                    unknown.add(f"document:{doc_id}:{key}")
    return [{"code": "confirm_unknown_fact", "field": key, "description": f"Confirm {key} from evidence; unknown is not zero or no.", "status": "pending"} for key in sorted(unknown)]


def clean_fields(kind: str, raw: dict[str, Any], schema: dict[str, str], money_fields: set[str], checkbox_fields: set[str]) -> tuple[dict[str, Any], list[str], list[str]]:
    """(fields, issues, security_flags) for one document.

    `schema` is the box map for a known kind; {} for a kind outside the profile,
    whose fields are kept (sanitised) so a person can see what was on it.
    """
    fields: dict[str, Any] = {}
    issues: list[str] = []
    flags: list[str] = []
    allowed = set(schema) | EXTRA_FIELDS.get(kind, set())
    for k in schema:
        v = raw.get(k)
        if k in money_fields:
            m = money(v)
            if kind == "1099-R" and k == "qcd_amount" and k not in raw:
                m = 0.0  # no optional QCD claim supplied; an explicit unreadable/null claim stays unknown
            if v not in (None, "") and m is None:
                issues.append(f"{k} ({schema[k]}) is not a readable amount: {str(v)[:40]!r}")
            elif m is not None and not (0 <= m <= MAX_AMOUNT):
                issues.append(f"{k} ({schema[k]}) is outside the plausible range: {m}")
                flags.append(f"{k}: implausible amount {m}")
                m = None
            fields[k] = m
        elif k in checkbox_fields:
            fields[k] = checkbox(v)
            if fields[k] is None:
                issues.append(f"{k} is unknown or unreadable; a person must confirm it")
        elif k == "employee_ssn_last4":
            fields[k] = _last4(v, issues, flags)
        else:
            fields[k] = _text(v, k, issues, flags)
            if fields[k] is not None and k in FORMATS and not FORMATS[k].match(fields[k]):
                issues.append(f"{k} ({schema[k]}) has an unexpected format: {fields[k][:30]!r}")
                fields[k] = None
    for k, v in raw.items():
        if k in schema or k in ("kind", "issues", "confidence", "injection_text"):
            continue
        if not isinstance(k, str) or not FIELD_KEY_RE.match(k):
            issues.append("a field with an invalid name was dropped")
            continue
        if schema and k not in allowed:
            issues.append(f"unexpected field {k!r} was dropped")
            continue
        if len(fields) >= 40:
            issues.append("too many fields; the rest were dropped")
            break
        m = money(v) if not isinstance(v, str) or re.fullmatch(r"\s*\$?[\d,]+(\.\d+)?\s*", v) else None
        fields[k] = m if m is not None else _text(v, k, issues, flags)
        if k in FORMATS and isinstance(fields[k], str) and not FORMATS[k].match(fields[k]):
            fields[k] = None
    flags.extend(injection.scan_values(fields))
    return fields, issues, flags


def _last4(v: Any, issues: list[str], flags: list[str]) -> str | None:
    if v in (None, ""):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{4}", s):
        return s
    found = pii.find_ssns(s)
    if found:
        flags.append("employee_ssn_last4: a full SSN was returned where only the last four digits belong; only those were kept")
        return found[0][-4:]
    issues.append("employee_ssn_last4 is not four digits; dropped")
    return None


# ---------------------------------------------------------------- HITL fact patches
FILING_STATUSES = ("single", "mfj", "mfs", "hoh", "qss")
PERSON_FIELDS = {"first_name": str, "last_name": str, "dob": str, "blind": bool, "ssn": str, "date_of_death": str}
PATCH_FIELDS: dict[str, Any] = {
    "filing_status": FILING_STATUSES, "married": bool, "spouse_died_in_year": bool, "can_be_claimed_as_dependent": bool,
    "documents_declared": list, "other_income_sources": list, "answering_party": ("self", "third_party_with_authority", "third_party_no_authority"),
    "third_party_authority": str, "refund_account_description": str, "refund_account_is_own": bool,
    "estimated_payments": float, "schedule1_adjustments": float, "schedule1_other_income": float, "schedule2_line3": float, "schedule2_line21": float, "schedule3_line8": float,
    "mfs_spouse_itemizes": bool, "dual_status_alien": bool, "taxpayer": dict, "spouse": dict,
    "dependents": list,
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clean_facts_patch(patch: Any) -> dict[str, Any]:
    """The patch, typed and limited to known facts, or ValueError with a message safe to show."""
    if not isinstance(patch, dict):
        raise ValueError("facts_patch must be a JSON object.")
    out: dict[str, Any] = {}
    for k, v in patch.items():
        spec = PATCH_FIELDS.get(k)
        if spec is None:
            raise ValueError(f"{k!r} is not a fact a resolution can change.")
        if isinstance(spec, tuple):
            if v not in spec:
                raise ValueError(f"{k} must be one of {', '.join(spec)}.")
            out[k] = v
        elif spec is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false.")
            out[k] = v
        elif spec is float:
            m = money(v)
            if m is None or not (0 <= m <= MAX_AMOUNT):
                raise ValueError(f"{k} must be an amount between 0 and {MAX_AMOUNT:,.0f}.")
            out[k] = m
        elif spec is str:
            out[k] = _patch_text(k, v)
        elif spec is list:
            if not isinstance(v, list) or len(v) > 12:
                raise ValueError(f"{k} must be a list (at most 12 items).")
            if k == "dependents":
                if any(not isinstance(x, dict) or set(x) - {"name", "relationship", "ssn"} for x in v):
                    raise ValueError("dependents must contain name, relationship and test-SSN objects.")
                out[k] = [{field: pii.require_test_ssn(value) if field == "ssn" else _patch_text(field, value) for field, value in x.items()} for x in v]
            else:
                out[k] = [_patch_text(k, x) for x in v]
        elif spec is dict:
            out[k] = _person(k, v)
    return out


def _patch_text(k: str, v: Any) -> str:
    if not isinstance(v, str) or len(v) > 200:
        raise ValueError(f"{k} must be text of at most 200 characters.")
    if pii.find_ssns(v):
        raise ValueError(f"{k} contains a Social Security number; SSNs go in the ssn field only.")
    return re.sub(r"[\x00-\x1f\x7f]+", " ", v).strip()


def _person(k: str, v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError(f"{k} must be an object.")
    out: dict[str, Any] = {}
    for f, val in v.items():
        typ = PERSON_FIELDS.get(f)
        if typ is None:
            raise ValueError(f"{k}.{f} is not a field a resolution can change.")
        if f == "ssn":
            try:
                out[f] = pii.require_test_ssn(val)
            except pii.RealSSNError as e:
                raise ValueError(f"{k}.ssn: {e}.") from None
        elif typ is bool:
            if not isinstance(val, bool):
                raise ValueError(f"{k}.{f} must be true or false.")
            out[f] = val
        else:
            if not isinstance(val, str) or len(val) > 80:
                raise ValueError(f"{k}.{f} must be text of at most 80 characters.")
            if f in ("dob", "date_of_death") and not DATE_RE.match(val):
                raise ValueError(f"{k}.{f} must be a date as YYYY-MM-DD.")
            out[f] = _patch_text(f"{k}.{f}", val)
    return out


# ---------------------------------------------------------------- checkpointed state
STATUSES = {"new", "intake", "extracting", "eligibility", "computing", "review", "waiting_human", "complete", "halted"}
ESCALATION_STATUSES = {"open", "resolved"}
LIST_KEYS = ("handoffs", "events", "documents_in", "escalations", "review_history", "token_log", "hitl_log")


def faults_allowed() -> bool:
    """The fault-injection test hook only works where the environment allows it (tests, evals)."""
    return os.environ.get("SILVERFILE_ALLOW_FAULTS", "0") == "1"


def check_state(values: dict[str, Any], thread_id: str) -> list[str]:
    """Problems with a state loaded from a checkpoint; [] when it is well formed."""
    from ..provenance import LINE_LABELS

    problems: list[str] = []
    if values.get("return_id") != thread_id:
        problems.append("return_id does not match the thread it was loaded from")
    if values.get("status") not in STATUSES:
        problems.append(f"unknown status {str(values.get('status'))[:20]!r}")
    if values.get("tax_year") not in (None, 2025):
        problems.append("tax year is not 2025")
    for key in LIST_KEYS:
        if key in values and not isinstance(values[key], list):
            problems.append(f"{key} is not a list")
    lines = values.get("lines") or {}
    if not isinstance(lines, dict):
        problems.append("lines is not a mapping")
        lines = {}
    for k, v in lines.items():
        if k not in LINE_LABELS:
            problems.append(f"unknown line {str(k)[:10]!r}")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            problems.append(f"line {k} is not a number")
    prov = values.get("provenance") or {}
    if not isinstance(prov, dict):
        problems.append("provenance is not a mapping")
        prov = {}
    for k, p in prov.items():
        if k not in LINE_LABELS or not isinstance(p, dict) or not isinstance(p.get("amount"), (int, float)):
            problems.append(f"malformed provenance record for {str(k)[:10]!r}")
    for e in values.get("escalations") or []:
        if not isinstance(e, dict) or not e.get("id") or e.get("status") not in ESCALATION_STATUSES:
            problems.append("malformed escalation record")
        elif e.get("status") == "resolved" and not (e.get("resolved_by") and e.get("resolved_at")):
            problems.append(f"escalation {e['id']} is resolved without a resolver and time")
    facts = values.get("intake") or {}
    people = [facts.get("taxpayer") or {}, facts.get("spouse") or {}, *(facts.get("dependents") or [])]
    for person in people:
        ssn = person.get("ssn") if isinstance(person, dict) else None
        if ssn and not pii.is_test_ssn(ssn):
            problems.append("a stored SSN is not a 900-series test number")
    if values.get("fault_injection") and not faults_allowed():
        problems.append("fault injection is set outside a test environment")
    for k in ("review_round", "computation_version"):
        if k in values and not (isinstance(values[k], int) and 0 <= values[k] <= 100):
            problems.append(f"{k} is out of range")
    event = values.get("blocking_review")
    if event is not None and (not isinstance(event, dict) or event.get("status") not in {"open", "resolved"} or not isinstance(event.get("required_actions"), list)):
        problems.append("malformed blocking review event")
    edit = values.get("human_edit")
    if edit is not None and (not isinstance(edit, dict) or edit.get("status") not in {"editing", "completed", "recomputed", "blocked"}):
        problems.append("malformed human edit state")
    if values.get("recompute_stage") not in (None, "eligibility", "computation", "review"):
        problems.append("unknown recomputation stage")
    if values.get("resolution_request") is not None and not isinstance(values["resolution_request"], dict):
        problems.append("malformed resolution request")
    for entry in values.get("hitl_log") or []:
        if not isinstance(entry, dict) or not entry.get("event") or entry.get("result") not in (None, "yes", "no"):
            problems.append("malformed human workflow log entry")
    return problems


# ---------------------------------------------------------------- every amount is sourced
USER_LINES = {"8", "10", "17", "20", "23", "26"}  # the only lines a taxpayer's stated amount may fill
FIXED_METHODS = {"none in profile", "no credit claimed in profile", "no refundable credits in profile", "out of scope (Schedule D); 0 by profile"}
SOURCE_KINDS = {"document", "computed", "user", "chart", "worksheet", "rate-schedule"}


def provenance_problems(state: dict[str, Any]) -> list[tuple[str, str]]:
    """(line, problem) for every amount on the draft that is not properly sourced.

    The structural guarantee that a model cannot put an unsourced number on the
    draft: Review fails on any of these, and finalize refuses to complete with one.
    """
    from ..provenance import LINE_LABELS

    lines = state.get("lines") or {}
    prov = state.get("provenance") or {}
    docs = set(state.get("extracted") or {})
    out: list[tuple[str, str]] = []
    for line, amount in lines.items():
        p = prov.get(line)
        if not p:
            out.append((line, "no provenance record"))
            continue
        if abs(float(p.get("amount", 0) or 0) - float(amount)) > 0.005:
            out.append((line, f"amount {amount} differs from its record ({p.get('amount')})"))
        kind, method = p.get("source_kind"), str(p.get("method") or "")
        if kind not in SOURCE_KINDS:
            out.append((line, f"unknown source kind {str(kind)[:20]!r}"))
        if kind == "user" and line not in USER_LINES:
            out.append((line, "a typed-in amount on a line that must come from a document or a computation"))
        if kind == "user" and method != "direct user input":
            out.append((line, "user-sourced amount without the direct-input method"))
        if kind != "user" and not (method.startswith("tax_math.") or method in FIXED_METHODS):
            out.append((line, f"method {method[:40]!r} is not a tax_math tool"))
        for src in p.get("sources") or []:
            if kind == "document" and src not in docs:
                out.append((line, f"source document {str(src)[:30]!r} is not on the return"))
            elif kind != "document" and src not in LINE_LABELS and src != "intake":
                out.append((line, f"source {str(src)[:30]!r} is neither a line nor intake"))
        if float(amount) != 0 and kind == "document" and not p.get("sources"):
            out.append((line, "a non-zero document amount with no source document"))
    return out
