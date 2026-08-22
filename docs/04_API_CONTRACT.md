# 04_API_CONTRACT.md

Standard error response shape (used by every endpoint below):
```json
{
  "error": {
    "code": "STRING_CODE",
    "message": "Human-readable explanation",
    "request_id": "uuid"
  }
}
```

---

# POST /api/auth/login

## Purpose
Authenticate a user and start a session.

## Authentication
Not required (this endpoint establishes it).

## Authorization
N/A.

## Request Body
```json
{ "email": "string", "password": "string" }
```

## Validation Rules
Both fields required, non-empty.

## Processing Behavior
Verify credentials, check lockout state, create session, set cookie.

## Success Responses
`200 OK`
```json
{ "user_id": "uuid", "role": "auditor|reviewer|admin", "name": "string" }
```

## Error Responses
`401` `INVALID_CREDENTIALS` (also used for unknown email — no enumeration)
`429` `TOO_MANY_ATTEMPTS` with `retry_after` field

## Rate Limits
5 failed attempts / 15 min per account triggers lockout (see 01_REQUIREMENTS.md).

## Idempotency
N/A (each call creates a new session).

## Side Effects
Creates a `Session` row; sets an httpOnly cookie.

## Security Notes
Constant-time credential comparison; identical error for wrong-password vs unknown-email.

---

# POST /api/engagements

## Purpose
Create a new engagement.

## Authentication
Required.

## Authorization
Role: auditor or reviewer.

## Request Body
```json
{
  "client_name": "string",
  "entity_type": "merchant|service_provider",
  "merchant_level": "1|2|3|4",
  "annual_transaction_volume": 0,
  "existing_saq_type": "string|null",
  "tech_stack_summary": "string|null",
  "source_document_ids": ["uuid"]
}
```

## Validation Rules
See 01_REQUIREMENTS.md → Engagement Creation.

## Processing Behavior
Creates Engagement (status=intake), auto-assigns creator via EngagementAssignment.

## Success Responses
`201 Created` — full Engagement object.

## Error Responses
`400 VALIDATION_ERROR` with field-level detail.

## Rate Limits
None beyond standard abuse protection.

## Idempotency
Not idempotent — repeated calls create separate engagements (this is intentional; a client may have multiple engagements).

## Side Effects
Creates Engagement + EngagementAssignment rows.

## Security Notes
`source_document_ids`, if present, are validated as belonging to the requesting user's firm (single-tenant in this POC, so this is a defensive check against ID-guessing, not a cross-tenant boundary).

---

# GET /api/engagements/{id}

## Purpose
Fetch one engagement.

## Authentication
Required.

## Authorization
User must be in `EngagementAssignment` for this engagement, OR hold role reviewer/admin. Enforced via a joined query, not a post-fetch check.

## Path Parameters
`id` (UUID).

## Success Responses
`200 OK` — Engagement object including current `status`, scoped requirement count, finding queue summary.

## Error Responses
`403 FORBIDDEN` if the user has no relationship to this engagement (not `404` — see Security Notes).
`404 NOT_FOUND` if the ID doesn't exist at all.

## Security Notes
Distinguishing 403 (exists, no access) from 404 (doesn't exist) is intentional here since this is single-tenant, internal-firm software, not a public multi-tenant system where existence-leakage would be sensitive — `DECISION REQUIRED` if you want to collapse both to 404 for extra caution.

---

# POST /api/engagements/{id}/scope-suggestion

## Purpose
Trigger AI scope matching (01_REQUIREMENTS.md → PCI DSS Scope Matching).

## Authentication
Required.

## Authorization
Must be assigned or reviewer/admin.

## Processing Behavior
Calls the scoping service; on LLM success returns proposed ScopedRequirement list; on LLM failure/timeout returns an empty proposal with `manual_scoping_required: true`. Always `200`, never `500`, for the LLM-unavailable case specifically.

## Success Responses
`200 OK`
```json
{ "proposed_requirements": [ { "clause_id": "1.2.1", "rationale": "string", "confirmed": false } ], "manual_scoping_required": false }
```

## Error Responses
`409 CONFLICT` `MISSING_PROFILE_FIELDS` if entity_type/merchant_level not set.

## Rate Limits
Capped per-user (e.g., 10/hour) to prevent runaway LLM cost from repeated triggering.

## Idempotency
Re-running replaces prior `ai_suggested, confirmed=false` rows; never touches rows already `confirmed=true`.

## Security Notes
Client evidence is not involved at this step — only structured profile fields are sent to the LLM.

---

# PATCH /api/scoped-requirements/{id}

## Purpose
Confirm or edit a proposed/manual scoped requirement.

## Authentication
Required.

## Authorization
Must be assigned to the parent engagement, or reviewer/admin.

## Request Body
```json
{ "confirmed": true }
```

## Success Responses
`200 OK` — updated ScopedRequirement.

## Security Notes
This is the human-confirmation gate described in 01_REQUIREMENTS.md — the API must not expose any way to bulk-confirm without each row being addressable individually in the audit trail (bulk UI action is fine; it must still write one confirmation event per row).

---

# POST /api/engagements/{id}/evidence-requests/generate

## Purpose
Generate the draft evidence checklist.

## Authentication
Required.

## Authorization
Must be assigned or reviewer/admin.

## Processing Behavior
See 01_REQUIREMENTS.md → Evidence Request Generation.

## Success Responses
`200 OK` — list of draft EvidenceRequest objects.

## Error Responses
`409 CONFLICT` `NO_CONFIRMED_SCOPE` if no ScopedRequirement has confirmed=true.

## Side Effects
Creates EvidenceRequest rows, status=draft. Never sends anything externally.

---

# POST /api/engagements/{id}/evidence-documents

## Purpose
Upload an evidence file.

## Authentication
Required.

## Authorization
Must be assigned or reviewer/admin.

## Request Body
`multipart/form-data`: `file` (binary), `evidence_request_id` (optional UUID).

## Validation Rules
MIME type in allow-list (checked by content inspection); size ≤ 25MB; filename sanitized.

## Processing Behavior
Stores file content-hash-addressed; creates EvidenceDocument row with `extraction_status=processing`; enqueues background extraction job.

## Success Responses
`201 Created` — EvidenceDocument object with `extraction_status: "processing"`.

## Error Responses
`400 UNSUPPORTED_FILE_TYPE`
`413 FILE_TOO_LARGE`

## Side Effects
Async: extraction → embedding → matching pipeline (see 02_ARCHITECTURE.md §7.5).

## Security Notes
File is never executed/rendered server-side beyond passive content extraction; no macro/script execution.

---

# GET /api/engagements/{id}/findings

## Purpose
Fetch the review queue.

## Authentication
Required.

## Authorization
Must be assigned or reviewer/admin.

## Query Parameters
`status` (optional filter: draft/approved/rejected), `needs_manual_review` (optional boolean filter).

## Success Responses
`200 OK` — array of Finding objects, each including `ai_suggested_status`, `ai_confidence`, `ai_rationale`, `evidence_document_ids`, `status`, `final_status`.

## Security Notes
Every Finding in the response must be visually/structurally distinguishable as `draft` vs `approved` — the API response schema itself (not just the UI) should make it impossible to mistake a draft AI suggestion for a final determination.

---

# PATCH /api/findings/{id}/review

## Purpose
Accept, edit, or reject a draft Finding (01_REQUIREMENTS.md → Finding Review).

## Authentication
Required.

## Authorization
Must be assigned (for own action) or reviewer (for any Finding, including overriding another user's prior action).

## Request Body
```json
{ "action": "accept|edit|reject", "edited_status": "satisfied|partial|not_satisfied|not_applicable", "note": "string" }
```

## Validation Rules
`edited_status` required if action=edit. `note` required if action=reject.

## Processing Behavior
Updates Finding.status/final_status, sets reviewed_by/reviewed_at, writes a FindingHistory row in the same transaction.

## Success Responses
`200 OK` — updated Finding object.

## Error Responses
`400 VALIDATION_ERROR` for missing conditional fields.

## Security Notes
`reviewed_by` is always set server-side from the authenticated session — never accepted from the request body.

---

# POST /api/engagements/{id}/finalize

## Purpose
Finalize the engagement and generate the report (01_REQUIREMENTS.md → Engagement Finalization).

## Authentication
Required.

## Authorization
**Reviewer role only.** Enforced server-side regardless of client UI state.

## Processing Behavior
Validates no draft Findings remain among confirmed scope (unless gap_acknowledged); snapshots approved Findings into a Report; sets Engagement.status=finalized.

## Success Responses
`200 OK`
```json
{ "report_id": "uuid", "engagement_status": "finalized" }
```

## Error Responses
`409 CONFLICT` `UNRESOLVED_FINDINGS` with a list of blocking ScopedRequirement IDs.
`403 FORBIDDEN` if caller is not a reviewer — this must be checked even if an Auditor somehow gets a finalize button rendered client-side.

## Idempotency
Calling finalize on an already-finalized engagement returns `409 ALREADY_FINALIZED` rather than creating a duplicate Report.

## Security Notes
This is the single highest-stakes endpoint in the system — the 06_ENGINEERING_RULES.md Definition of Done should require an explicit test for the 403 case on this endpoint specifically before any PR touching it is considered complete.

---

# Endpoints added by ADR-012

The following endpoints implement capabilities that 00_PRODUCT.md §5.3 or 03_DATA_MODEL.md §8.2 already grant but for which this contract originally defined no path. All of them are subject to the same standard error envelope, the same authentication requirement, and the same query-level ownership filtering as the endpoints above.

---

# POST /api/auth/logout

## Purpose
End the current session.

## Authentication
Required.

## Processing Behavior
Sets `revoked_at` on the current Session row and clears the cookie. Idempotent — calling it without a valid session still returns `204`, since the desired end state (no session) is already true.

## Success Responses
`204 No Content`.

## Security Notes
Revocation is server-side. Clearing the cookie alone would leave a stolen token valid for its remaining lifetime.

---

# GET /api/auth/me

## Purpose
Return the authenticated user's own identity and role, so the frontend can render role-appropriate UI.

## Authentication
Required.

## Success Responses
`200 OK` — `{ "user_id", "email", "name", "role" }`.

## Security Notes
This is a convenience for rendering, never an authorization source. Every protected endpoint re-derives the role server-side regardless of what the client learned here (05_SECURITY.md §10.3).

---

# GET /api/engagements

## Purpose
List engagements visible to the caller.

## Authentication
Required.

## Authorization
Auditors see only engagements they are assigned to; Reviewers and Admins see all. Enforced by the same query-level join as `GET /api/engagements/{id}`, not by filtering a full list in application code.

## Query Parameters
`status` (optional filter), `limit` (default 50, max 200), `offset` (default 0).

## Success Responses
`200 OK` — `{ "items": [Engagement], "total": integer }`.

---

# POST /api/engagements/{id}/assignments

## Purpose
Assign a user to an engagement (03_DATA_MODEL.md → EngagementAssignment: "only a Reviewer or Admin can add/remove assignments").

## Authentication
Required.

## Authorization
Role reviewer or admin only.

## Request Body
```json
{ "user_id": "uuid" }
```

## Success Responses
`201 Created` — the assignment object.

## Error Responses
`403 FORBIDDEN` if the caller is an auditor.
`404 NOT_FOUND` if the engagement or target user does not exist.
`409 CONFLICT` `ALREADY_ASSIGNED` — the unique constraint on (`engagement_id`, `user_id`) is reported, not swallowed.
`409 CONFLICT` `ENGAGEMENT_FINALIZED` if the engagement is already finalized.

## Security Notes
This endpoint grants access to another organisation's audit data, so it is Reviewer/Admin-only and every call is logged with actor, target user, and engagement.

---

# DELETE /api/engagements/{id}/assignments/{user_id}

## Purpose
Remove a user's assignment.

## Authentication
Required.

## Authorization
Role reviewer or admin only.

## Success Responses
`204 No Content`.

## Error Responses
`403 FORBIDDEN`, `404 NOT_FOUND`, `409 ENGAGEMENT_FINALIZED`.

## Security Notes
Removing an assignment revokes access immediately on the next request — authorization is re-derived per request and never cached in the session.

---

# PATCH /api/scoped-requirements/{id}/gap

## Purpose
Acknowledge that a confirmed requirement will be finalized without an approved Finding (01_REQUIREMENTS.md → Engagement Finalization: "must have an explicit `gap_acknowledged = true` flag set by the Reviewer").

## Authentication
Required.

## Authorization
**Reviewer role only** — this is the flag that permits finalization despite missing evidence, so it carries the same authority as finalization itself.

## Request Body
```json
{ "gap_acknowledged": true, "gap_note": "string" }
```

## Validation Rules
`gap_note` is required and non-empty when `gap_acknowledged` is true. An acknowledged gap must be explainable, for the same reason a rejected Finding must be.

## Success Responses
`200 OK` — the updated ScopedRequirement.

## Error Responses
`400 VALIDATION_ERROR` if the note is missing.
`403 FORBIDDEN` if the caller is not a Reviewer.
`409 ENGAGEMENT_FINALIZED`.

---

# POST /api/client-profile-documents

## Purpose
Upload a firm-internal client-file document that can later be referenced by `source_document_ids` at engagement creation (ADR-011 item 6).

## Authentication
Required.

## Authorization
Role auditor, reviewer, or admin.

## Request Body
`multipart/form-data`: `file` (binary).

## Validation Rules
Identical to evidence upload: MIME allow-list by content inspection, size ≤ 25MB, filename sanitized.

## Success Responses
`201 Created` — the ClientProfileDocument object.

## Error Responses
`400 UNSUPPORTED_FILE_TYPE`, `413 FILE_TOO_LARGE`.

## Security Notes
These are firm-held documents, not client-submitted evidence, so they are not attached to an engagement and are not routed into the extraction/matching pipeline.

---

# GET /api/engagements/{id}/scoped-requirements
# GET /api/engagements/{id}/evidence-requests
# GET /api/engagements/{id}/evidence-documents

## Purpose
Read the engagement's scope, checklist, and uploaded evidence. The contract defined write paths for all three without a corresponding read path.

## Authentication
Required.

## Authorization
Must be assigned, or reviewer/admin — the same joined-query filter as every other engagement-scoped endpoint.

## Success Responses
`200 OK` — array of the respective objects.

## Security Notes
`EvidenceDocument.extracted_text` and `storage_path` are Sensitive (03_DATA_MODEL.md §8.4). The list response omits `extracted_text` and never returns `storage_path`; the full text is available only through the single-document read, and the raw file only through the download endpoint below.

---

# GET /api/evidence-documents/{id}/download

## Purpose
Retrieve the original uploaded file.

## Authentication
Required.

## Authorization
Must be assigned to the parent engagement, or reviewer/admin.

## Processing Behavior
Streams the stored file with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.

## Security Notes
The response is always an attachment, never inline — an uploaded HTML or SVG file rendered inline in the app's own origin would be a stored-XSS vector. `storage_path` is never exposed to the client; the file is addressed by document ID and resolved server-side.

---

# PATCH /api/evidence-requests/{id}

## Purpose
Edit a draft request's description, or mark it `sent_externally` / `received` as the auditor's note-to-self (ADR-004 — the system never verifies actual sending).

## Authentication
Required.

## Authorization
Must be assigned, or reviewer/admin.

## Request Body
```json
{ "description": "string|null", "status": "draft|sent_externally|received|null" }
```

## Success Responses
`200 OK` — the updated EvidenceRequest.

---

# GET /api/engagements/{id}/report

## Purpose
Retrieve the finalized report snapshot, and its PDF export.

## Authentication
Required.

## Authorization
Must be assigned, or reviewer/admin.

## Query Parameters
`format` — `json` (default) returns the snapshot; `pdf` streams the generated export.

## Success Responses
`200 OK`.

## Error Responses
`404 NOT_FOUND` if the engagement has not been finalized.

---

# Admin user management

`POST /api/admin/users`, `GET /api/admin/users`, `PATCH /api/admin/users/{id}`

## Purpose
Account provisioning and deactivation — 00_PRODUCT.md §5.3 grants the Admin role "create/deactivate user accounts", and 01_REQUIREMENTS.md states accounts are "provisioned by an Admin only", with no endpoint previously defined.

## Authentication
Required.

## Authorization
**Role admin only.**

## Request Body (create)
```json
{ "email": "string", "name": "string", "role": "auditor|reviewer|admin", "password": "string" }
```

## Validation Rules
Email unique and well-formed; password minimum 12 characters; role must be one of the three enum values.

## Processing Behavior
`PATCH` supports `is_active` and `role` only. Users are deactivated, never deleted (03_DATA_MODEL.md → User lifecycle).

## Error Responses
`403 FORBIDDEN` for any non-admin caller.
`409 CONFLICT` `EMAIL_ALREADY_EXISTS`.

## Security Notes
This is the only path by which `role` is ever set (05_SECURITY.md §10.3: "role is set only by Admin action on the User record, never accepted as a client-supplied field on any other endpoint"). An Admin granting themselves the reviewer role does not bypass ADR-003 — sign-off authority is a role property, and the grant itself is logged.
