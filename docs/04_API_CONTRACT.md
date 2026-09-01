# 04_API_CONTRACT.md

> Path prefix changes from `/engagements` to `/audits` throughout. Endpoints unchanged in substance from the prior revision (auth, audit creation, scope confirmation, evidence-request generation, evidence upload) are listed briefly; new/changed endpoints (control corpus, facts, evaluation, gate, findings, finalize) are specified in full.

Standard error shape unchanged:
```json
{ "error": { "code": "STRING_CODE", "message": "string", "request_id": "uuid" } }
```

---

# POST /api/auth/login
Unchanged from the prior revision.

# POST /api/audits
Same as prior revision's `POST /api/engagements`, renamed, `test_company` field added to the request body.

# GET /api/audits/{id}
Same as prior revision, renamed.

# POST /api/audits/{id}/scope-suggestion
Same as prior revision, renamed. Note: still advisory/human-confirmed — this endpoint proposes which `ControlDefinition`s apply; it does not touch evaluation.

# PATCH /api/scoped-controls/{id}
Same as prior revision's `PATCH /api/scoped-requirements/{id}`, renamed.

# POST /api/audits/{id}/evidence-requests/generate
Same as prior revision, renamed.

# POST /api/audits/{id}/evidence-documents
Same as prior revision, renamed, with one addition: response now includes `malware_scan_status` alongside `extraction_status` (value `not_scanned` if the Level 0 decision is to log-only rather than gate — see 00_PRODUCT.md `DECISION REQUIRED`).

---

# GET /api/control-definitions

## Purpose
List the machine-readable control corpus (for scoping, authoring, and display).

## Authentication
Required.

## Authorization
Any authenticated user (read); write access is a separate Admin-only endpoint below.

## Query Parameters
`evaluation_mode`, `requirement_family`, `corpus_version`.

## Success Responses
`200 OK` — array of `ControlDefinition` objects, including `facts` and `rules` for DETERMINISTIC/STRUCTURED controls (visible to any authenticated user — these are not secret, they're the published standard's mechanics).

---

# POST /api/control-definitions (Admin only)

## Purpose
Author or version a control definition.

## Authorization
Admin role only.

## Request Body
```json
{
  "control_id": "8.3.1",
  "name": "Minimum Password Length",
  "requirement_text": "string",
  "evaluation_mode": "DETERMINISTIC",
  "evidence_requirements": [ { "type": "configuration", "description": "string" } ],
  "facts": [ { "name": "minimum_password_length", "type": "integer" } ],
  "rules": [ { "fact": "minimum_password_length", "operator": ">=", "expected": 12 } ],
  "freshness_window_days": 90
}
```

## Validation Rules
`evaluation_mode=DETERMINISTIC` requires non-empty `facts` and `rules` (01_REQUIREMENTS.md → Machine-Readable Control Definition) — enforced here, not just at the database layer.

## Success Responses
`201 Created`.

## Error Responses
`400 VALIDATION_ERROR` `DETERMINISTIC_CONTROL_MISSING_RULES` — a specific, named error code precisely because this is the single most important validation in the system.

## Security Notes
This endpoint's write access is the one place in the whole API where a human directly authors what the rule engine will later treat as ground truth — it must never be reachable by anything other than a real Admin action, and never by any AI-assisted "auto-populate rules" shortcut.

---

# GET /api/audits/{id}/facts

## Purpose
Inspect extracted facts for an audit (primarily for review/debugging and for the Finding review screen's evidence display).

## Authorization
Must be assigned to the audit, or reviewer/admin.

## Query Parameters
`control_definition_id`, `verification_status`.

## Success Responses
`200 OK` — array of `EvidenceFact` objects with full provenance (`document_id`, `page`/`line`/`cell`, `source_hash`, `extracted_at`).

## Security Notes
`source_hash` is included specifically so a client (or a test) can verify it still matches the current document's hash — a mismatch is a first-class, checkable condition, not just an internal detail.

---

# POST /api/audits/{id}/evaluate

## Purpose
Trigger (or re-trigger) rule-engine evaluation for the audit's scoped controls.

## Authorization
Must be assigned to the audit, or reviewer/admin.

## Processing Behavior
Runs the rule engine (01_REQUIREMENTS.md → Deterministic Rule Evaluation) against current `EvidenceFact` rows for each `ScopedControl`, then the Evidence Gate, producing new `ControlEvaluation` rows (never edits existing ones — see 03_DATA_MODEL.md).

## Success Responses
`200 OK` — array of new `ControlEvaluation` objects, each with `result` and `gate_status`.

## Error Responses
None specific — a control with no facts yet simply evaluates to `INSUFFICIENT_EVIDENCE`, which is a valid result, not an error.

## Security Notes
This endpoint **never** accepts an LLM-related parameter of any kind (no "confidence threshold," no "let AI decide ties") — its entire contract is mechanical inputs, mechanical output.

---

# GET /api/audits/{id}/findings

## Purpose
Fetch the review queue — now explicitly surfacing both the mechanical and human-decision fields.

## Success Responses
`200 OK`
```json
{
  "findings": [
    {
      "id": "uuid",
      "control_id": "8.3.1",
      "requirement_text": "string",
      "system_result": "PASS",
      "gate_status": "VERIFIED",
      "evidence_locations": [ { "document": "iam_config.pdf", "page": 7 } ],
      "ai_explanation": "string, clearly labeled non-authoritative",
      "status": "pending_review",
      "auditor_decision": null
    }
  ]
}
```

## Security Notes
The response schema itself keeps `system_result` (machine) and `auditor_decision` (human, nullable until reviewed) as separate top-level fields — this is a deliberate API-contract-level enforcement of the invariant in 01_REQUIREMENTS.md → Finding Review, Explicitly Forbidden Behavior, not just a documentation note.

---

# PATCH /api/findings/{id}/review

## Purpose
Record an auditor's decision (01_REQUIREMENTS.md → Finding Review).

## Request Body
```json
{ "auditor_decision": "PASS|FAIL|PARTIAL|INSUFFICIENT_EVIDENCE|CONFLICT|NOT_APPLICABLE", "note": "string" }
```

## Validation Rules
`note` required if `auditor_decision` differs from the wrapped `ControlEvaluation.result` (an override) or if `status` is being set to `rejected`/`needs_more_evidence`.

## Success Responses
`200 OK` — updated Finding, `system_result` unchanged, `auditor_decision` now set.

## Security Notes
`system_result` is never accepted in this request body — there is no field name that could even be misused to overwrite it, since the schema for this endpoint doesn't include it.

---

# POST /api/audits/{id}/finalize
Same invariant as the prior revision (Reviewer-only, blocks on unresolved Findings, immutable snapshot) — snapshot now includes both `system_result` and `auditor_decision` per control, plus `engine_version`/`corpus_version` (03_DATA_MODEL.md → Report).
