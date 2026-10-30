# Guardrails A: evidence conflicts and transcription recovery

The workflow is usable through the current staff dashboard, without new intake questions. Only preparers and admins can inspect original sources or submit document corrections. Existing role checks, CSRF, interrupt tokens, request limits and audit redaction remain in force.

## Source-linked statements

`intake.document_claims` is a list of comparisons, not a route for writing return lines:

```json
{"document_claims":[{"document_id":"w2_image_1","kind":"W-2","field":"wages","value":35000}]}
```

The document ID is the ID displayed on the current return. Supported kinds are W-2, SSA-1099, 1099-R, 1099-INT and 1099-DIV; allowed monetary fields are listed in `reconciliation.CLAIM_FIELDS`. Without a document ID, exactly one active document of the selected kind is required. Multiple possible documents require staff to identify the source; the claim is not compared with an arbitrary document or an aggregate total.

A narrow explicit phrase such as `My W-2 wages were $35,000` is captured from existing answers independently of model parsing. General free text and different scans of potentially corrected forms are not universally reconciled. A model cannot invent claims through its slot parser. Additional deterministic capture patterns or future intake questions can use the same structured contract.

Eligibility exposes mismatches with both values, field/box references, document versions and a required action. The dashboard and `/returns/<id>/hitl.json` expose the same structured conflicts. Computation, continuation and finalization reject unresolved conflicts, including when old eligibility results exist. Staff can correct a mistaken claim using Facts JSON, with a written evidence note; previous and corrected claims are logged.

## Correcting a transcription

On a paused return, **Document transcription review** shows each active extracted record, its box schema, version and an authenticated original-source download.

1. The preparer checks the original source and enters only the incorrect box fields as a JSON object.
2. An evidence explanation identifies the source, boxes and reason. The request carries the current document version and interrupt token.
3. `correct_document` creates a checkpointed work order. Only Extraction applies its normalized values; the original file is unchanged. Unrelated documents are not re-read for this correction.
4. Old calculations, provenance and acceptance are invalidated. Extraction is followed by eligibility, computation and review, then a deterministic resolution check. Unsafe or incomplete inputs remain paused.
5. The log records the actor, timestamp, prior and corrected values, explanation and resulting document version. Document-derived provenance includes the versions used. Work orders survive restart and subsequent automatic extraction rounds.

The service/CLI payload is:

```json
{
  "action":"correct_document",
  "document_patch":{
    "document_id":"w2_image_1",
    "expected_version":1,
    "fields":{"wages":35000},
    "evidence":"Preparer checked the original W-2, Box 1, and confirmed the transcription correction."
  }
}
```

Amounts here are illustrative, not instructions to change a particular fixture's value. Actual corrections follow the source. Invalid JSON, wrong types, unknown fields, missing evidence or stale document versions are rejected before graph mutation. The form retains invalid input for correction. Security findings cannot be cleared by editing fields; those cases require a preparer handoff. True box 2b or other actual scope exclusions still cannot be approved by supplying a taxable amount or clicking Accept. An actual misread checkbox can be corrected only through the authenticated, evidence-backed process.

For identical-source duplicates, the work order can contain `fields:{}` and `superseded_by` naming another active copy. Matching upload hashes or an identical stored source establish the relationship. The excluded copy remains in state and the audit trail, but is not counted again. Different files from the same payer and identical structured values without source identity are not sufficient proof of duplication.

A correction does not bypass an existing blocking review event: explicit event resolution, a saved intervention explanation, and Accept are still separate required steps.

## Negative and unknown answers

Deterministic checks distinguish explicit negatives from uncertainty and mixed answers. They prevent `I am not blind` becoming true, a negated/pending/uncertain permission statement becoming authority, and unknown other income or document inventory becoming empty lists. Earlier dependents remain intact if the next dependent is uncertain. Explicit no/none/zero still works; conflicting amounts or unsupported monetary text require clarification instead of a guessed number. The Schedule 1 prompt no longer suggests answering none when the person does not know.

These controls run after model parsing as well as in the offline parser. They do not independently verify a power of attorney, prove source authenticity, or recognize every linguistic variation.

## Integration with Guardrails B

The existing deduction-chart contract in `INTAKE_DIGNITY.md` is unchanged. Active questions for `dual_status_alien` and MFS-only `mfs_spouse_itemizes` remain pending.

Future intake wording can preserve `null` and `_unknown_fields` for uncertainty, and confirmed answers can clear the corresponding marker. `other_income_sources`, `documents_declared`, `spouse_died_in_year` and additional dependents now also retain explicit unknown states. New yes/no slots can use the shared `parse_yes_no` helper and the same post-model check. Known positives and negatives still need conversation-level validation after new questions are connected.

## Minimal acceptance

1. A monetary claim differing from its named source shows both values. Continue without correction remains paused with `result:"no"`.
2. A valid source transcription correction updates that document's version, preserves the original upload, and recalculates from the corrected values. A mistaken claim can instead be corrected with an evidence note.
3. Wrong JSON or a stale version leaves the saved state unchanged; the same form can be corrected and submitted again.
4. Two copies of one source require an explicit duplicate disposition and contribute income only once. Two distinct W-2s continue to contribute separately.
5. `I'm her daughter and I do not have permission` pauses for exploitation review. `No, I am not legally blind` records false. Unknown other income remains unknown until confirmed.

Automated coverage: `tests/test_reconciliation.py`, `tests/test_guardrails_semantics.py`, the existing HITL/security suite, and the deduction-chart matrix in `tests/test_escalations.py`. All fixtures are synthetic. Live models and a human click-through are separate validation activities.

Validation snapshot, October 30, 2026 (Windows, Python 3.12.14, strict contracts, offline): 93 focused regressions passed; the full suite on the merged master baseline had 598 passed and 2 existing Windows/POSIX key-file permission assertion failures; the ten business evaluations all passed. The two permission-bit assertions are intentionally retained for potential Linux deployment; Linux success has not been verified. The user completed the isolated synthetic manual acceptance workflow before authorizing commit and push. No live-model validation is claimed.
