# 01_REQUIREMENTS.md

---

# Feature: User Authentication

## Purpose
Establish who is using the system before any engagement data is accessible.

## Actors
Auditor, Reviewer, Admin (all authenticate the same way; role is a property of the account, not the login method).

## Preconditions
Account must already exist (created by an Admin — no public self-registration).

## Trigger
User submits email + password on the login form.

## Inputs
`email` (string, required), `password` (string, required).

## Validation Rules
- Email must match an existing active account.
- Password checked against the stored Argon2id hash.
- No user-enumeration signal: invalid email and invalid password return the identical error message and identical response timing (constant-time comparison).

## Processing Rules
- On success, create a server-side session record, set an httpOnly, Secure, SameSite=Strict session cookie.
- On 5 consecutive failed attempts for one account within 15 minutes, lock that account's login for 15 minutes and log the event.

## Business Rules
No self-registration. Accounts are provisioned by an Admin only.

## Authorization Rules
N/A (this endpoint is the authorization boundary itself).

## Database Effects
Creates a `Session` record on success. Creates/updates a `LoginAttempt` counter on failure.

## External Dependencies
None.

## Success Output
`200 OK`, session cookie set, returns `{ "user_id", "role", "name" }` — never the password hash, never other users' data.

## Failure Cases
- Invalid credentials → `401`, generic message, no indication of which field was wrong.
- Account locked → `429` with a `retry_after` value, no indication of why beyond "too many attempts."
- Account deactivated → same generic `401` as invalid credentials (do not reveal account existence/state).

## Edge Cases
- Session already active elsewhere: allowed (no single-session enforcement in POC) — `DECISION REQUIRED` if this needs to change.
- Password reset: out of scope for POC; Admin manually resets via direct database action documented in an internal runbook, not a self-service flow. `DECISION REQUIRED` if this is unacceptable.

## Non-Functional Requirements
Session TTL: 8 hours idle timeout, 24 hours absolute maximum, matching a typical auditor's working day.

## Acceptance Criteria
- Given valid credentials, when POST /api/auth/login is called, the response is 200 and a session cookie is set.
- Given an invalid password, when POST /api/auth/login is called, the response is 401 with a generic error and no session cookie is set.
- Given 5 failed attempts in 15 minutes, when a 6th attempt is made with the correct password, the response is 429.

## Explicitly Forbidden Behavior
The API must never return whether the email exists but the password was wrong versus the email not existing at all.

---

# Feature: Engagement Creation & Client Profile Intake

## Purpose
Create the working record for one PCI DSS assessment engagement, seeded from information the audit firm already holds on the client.

## Actors
Auditor, Reviewer.

## Preconditions
User is authenticated and holds Auditor or Reviewer role.

## Trigger
User submits "New Engagement" form.

## Inputs
`client_name` (string, required), `entity_type` (enum: merchant / service_provider, required), `merchant_level` (enum: 1/2/3/4, required if entity_type=merchant), `annual_transaction_volume` (integer, optional), `existing_saq_type` (enum, optional, nullable), `tech_stack_summary` (free text, optional), `source_document_ids` (array of previously-uploaded firm-internal document references, optional).

## Validation Rules
- `client_name` non-empty, max 200 chars.
- `merchant_level` required and validated only when `entity_type = merchant`.
- `source_document_ids`, if provided, must reference documents the requesting user's firm already owns (not arbitrary IDs).

## Processing Rules
System does NOT attempt to connect to any external client system to gather this data — all inputs come from the form or from documents the firm already has on file and is uploading/pointing to.

## Business Rules
An engagement always belongs to exactly one client and is scoped to PCI DSS v4.0.1 only (framework field is fixed, not user-selectable, in this POC).

## Authorization Rules
Any Auditor or Reviewer at the firm may create an engagement. The creator is automatically the first assigned Auditor.

## Database Effects
Creates one `Engagement` row, status = `intake`. Optionally links existing `ClientProfileDocument` rows.

## External Dependencies
None at creation time (the scope-matching LLM call happens in the next feature, not here).

## Success Output
`201 Created`, returns the new `Engagement` object with status `intake`.

## Failure Cases
- Missing required field → `400` with field-level error detail.
- `entity_type = merchant` but no `merchant_level` → `400`.

## Edge Cases
- Duplicate client name: allowed (same client may have multiple engagements over time, e.g. annual re-assessment) — engagements are distinguished by ID and date, not uniqueness on name.

## Non-Functional Requirements
Creation must complete in under 500ms (no external calls in this step).

## Acceptance Criteria
- Given a valid payload, when POST /api/engagements is called, an Engagement is created with status `intake` and the creating user is in its assigned-auditors list.
- Given `entity_type=merchant` and no `merchant_level`, when POST /api/engagements is called, the response is 400 and no Engagement row is created.

## Explicitly Forbidden Behavior
The system must never attempt an outbound network call to any domain associated with the client during this step — this feature is explicitly firm-internal-data-only.

---

# Feature: PCI DSS Scope Matching

## Purpose
Given a client profile, propose which PCI DSS v4.0.1 requirements and SAQ type apply, so the auditor isn't manually re-deriving scope from the standard every time.

## Actors
Auditor, Reviewer (trigger); system (LLM-assisted matching).

## Preconditions
Engagement exists with `entity_type` and (if applicable) `merchant_level` set.

## Trigger
Auditor requests "Suggest Scope" on an engagement in `intake` status.

## Inputs
The Engagement's profile fields; the current PCI DSS v4.0.1 corpus (see 03_DATA_MODEL.md).

## Validation Rules
Engagement must have the minimum profile fields set (see Engagement Creation).

## Processing Rules
1. Retrieve candidate SAQ types / applicable requirement families from the corpus based on `entity_type`, `merchant_level`, `tech_stack_summary`.
2. LLM call proposes: SAQ type, list of applicable requirement IDs, and a plain-language rationale per major inclusion/exclusion.
3. Result is stored as `proposed`, never auto-accepted.

## Business Rules
The proposed scope is always editable by the auditor before it's used to drive evidence requests. The system's suggestion is advisory, not authoritative.

## Authorization Rules
Same as engagement access — user must be assigned or be a Reviewer.

## Database Effects
Creates `ScopedRequirement` rows with `source = "ai_suggested"`, `confirmed = false`. Auditor edits toggle `confirmed` and can add/remove rows (`source = "manual"` for anything the auditor added directly).

## External Dependencies
LLM API (see 02_ARCHITECTURE.md §7.6 for timeout/retry/fallback behavior).

## Success Output
`200 OK`, list of proposed `ScopedRequirement` objects, each with a rationale string.

## Failure Cases
- LLM call times out or errors → engagement remains in `intake`, response indicates the auditor must scope manually via the corpus browser; this is not a hard failure of the endpoint, it degrades gracefully.

## Edge Cases
- Ambiguous entity type (e.g., a payment aggregator not clearly merchant or service provider) → system proposes the broader/stricter scope and flags it `needs_manual_review = true` rather than guessing narrow.

## Non-Functional Requirements
LLM call has an 8-second timeout with graceful degradation (see above); never leaves the user waiting indefinitely.

## Acceptance Criteria
- Given a merchant_level=4 SAQ-A-eligible profile, when scope suggestion runs, the response includes a proposed SAQ type and at least one requirement family, none marked `confirmed`.
- Given an LLM timeout, when scope suggestion is requested, the response is 200 with an empty proposal and a `manual_scoping_required: true` flag — never a 500.

## Explicitly Forbidden Behavior
The system must never mark a `ScopedRequirement` as `confirmed = true` without an explicit human action.

---

# Feature: Evidence Request Generation

## Purpose
Turn the confirmed scope into a concrete, human-reviewable checklist of what's missing, so the auditor doesn't manually cross-reference 78 requirements against what's already on file.

## Actors
Auditor, Reviewer.

## Preconditions
Engagement has at least one `ScopedRequirement` with `confirmed = true`.

## Trigger
Auditor requests "Generate Evidence Checklist."

## Inputs
Engagement ID.

## Validation Rules
At least one confirmed scoped requirement must exist, else reject with a clear message.

## Processing Rules
For each confirmed requirement, check existing `EvidenceDocument` links; for anything unmet, draft an `EvidenceRequest` with a plain-language description of what's needed (not just the clause number).

## Business Rules
Generated requests are always `status = draft` — they are never sent anywhere by the system itself in this POC (see Product Challenge #1).

## Authorization Rules
User must be assigned to the engagement.

## Database Effects
Creates `EvidenceRequest` rows, `status = draft`.

## External Dependencies
LLM call to draft the plain-language description (falls back to a template-based description referencing the raw clause text if the LLM call fails — this feature must never fail outright).

## Success Output
`200 OK`, list of draft `EvidenceRequest` objects, editable by the auditor.

## Failure Cases
No confirmed scope → `409 Conflict` with guidance to complete scoping first.

## Edge Cases
Re-running generation after some evidence has already arrived: only drafts requests for genuinely still-missing items, does not duplicate requests for items already satisfied.

## Non-Functional Requirements
None beyond standard API latency targets (see 02_ARCHITECTURE.md §7.9).

## Acceptance Criteria
- Given a confirmed scope with 40 requirements and evidence already on file for 10, when checklist generation runs, exactly 30 draft EvidenceRequest rows are created (assuming 1:1 requirement-to-request for this example).

## Explicitly Forbidden Behavior
The system must never dispatch an email, message, or any external communication to the client as part of this feature.

---

# Feature: Evidence Document Ingestion & Extraction

## Purpose
Get client-submitted documents (received by the auditor through their own channel) into a structured, searchable form.

## Actors
Auditor, Reviewer.

## Preconditions
Engagement exists; optionally an `EvidenceRequest` the document is being submitted against.

## Trigger
Auditor uploads one or more files.

## Inputs
File(s): PDF, DOCX, XLSX, PNG, JPG. Max 25MB per file. Optional link to an `EvidenceRequest` ID.

## Validation Rules
- MIME type validated server-side by content inspection, not by filename extension alone.
- Size limit enforced server-side.
- Filename sanitized before storage (no path traversal characters).

## Processing Rules
1. Store the original file (immutable, content-hash-addressed).
2. Run extraction (text for PDFs/DOCX, structured cells for XLSX, OCR for images/scanned PDFs).
3. On successful extraction, proceed to the matching pipeline (next feature). On failure, mark `extraction_failed` and stop — no partial/garbled data proceeds to matching.

## Business Rules
Original uploaded files are never deleted or overwritten (append-only) — this is the evidentiary record.

## Authorization Rules
User must be assigned to the engagement.

## Database Effects
Creates `EvidenceDocument` row (metadata + storage reference + extraction status), and if linked, updates the referenced `EvidenceRequest.status` to `received`.

## External Dependencies
Extraction/OCR pipeline (self-hosted, see 02_ARCHITECTURE.md).

## Success Output
`201 Created`, `EvidenceDocument` object with `extraction_status`.

## Failure Cases
- Unsupported file type → `400`, no file stored.
- File exceeds size limit → `413`.
- Extraction fails (corrupt file, unreadable scan) → file IS stored (it's still evidence), but `extraction_status = failed`, and the auditor is shown a manual-review prompt rather than a fabricated finding.

## Edge Cases
Password-protected PDFs: rejected with a specific error asking the auditor to obtain an unprotected copy — the system does not attempt to guess or brute-force passwords.

## Non-Functional Requirements
Extraction pipeline must run asynchronously (background job) for files over ~5 pages so the upload request itself doesn't block; upload endpoint returns immediately with `extraction_status = processing`.

## Acceptance Criteria
- Given a valid PDF upload, the response is 201 and, within a reasonable async window, `extraction_status` transitions from `processing` to `complete` or `extraction_failed`.
- Given a .exe file renamed to .pdf, when uploaded, content-type inspection rejects it with 400.

## Explicitly Forbidden Behavior
The system must never execute, render, or open uploaded files in a way that could trigger embedded active content (macros, scripts) — extraction must use passive parsing libraries only.

---

# Feature: Evidence-to-Clause Matching (Draft Finding Generation)

## Purpose
Produce a first-pass, human-reviewable judgment of whether a piece of evidence satisfies a specific PCI DSS clause.

## Actors
System (triggered automatically after successful extraction); Auditor/Reviewer (consumers of the output).

## Preconditions
`EvidenceDocument.extraction_status = complete`; Engagement has confirmed scope.

## Trigger
Automatic, immediately following successful extraction.

## Inputs
Extracted document content; the confirmed `ScopedRequirement` set and their corpus clause text.

## Validation Rules
N/A (internal pipeline step).

## Processing Rules
1. Chunk and embed the extracted content.
2. Retrieve the top-matching clause(s) via vector similarity against the scoped requirement set only (never against the full corpus — this bounds the matching to what's actually relevant to this engagement).
3. LLM call: given the evidence chunk and the candidate clause text, produce a suggested status (`satisfied` / `partial` / `not_satisfied` / `not_applicable`), a confidence score (0–1), a one-paragraph rationale, and an explicit citation (document + page/location, clause ID).
4. If confidence < 0.6, set `needs_manual_review = true` regardless of suggested status.

## Business Rules
Every Finding is created with `status = draft`. No Finding is ever auto-approved.

## Authorization Rules
Read/write access to resulting Findings follows standard engagement assignment rules.

## Database Effects
Creates one or more `Finding` rows, each linked to one `ScopedRequirement` and one or more `EvidenceDocument` citations.

## External Dependencies
Embedding model, vector similarity search, LLM API.

## Success Output
Finding objects appear in the engagement's review queue with `status = draft`.

## Failure Cases
LLM call fails → Finding is still created with `status = draft`, `ai_suggestion = null`, `needs_manual_review = true` — the auditor sees "no AI suggestion available, manual review needed" rather than a missing row or a fabricated guess.

## Edge Cases
One piece of evidence can satisfy multiple clauses (e.g., one firewall config screenshot може cover several network-security requirements) — the matching step must be able to produce multiple Findings from one document.

## Non-Functional Requirements
This is the most compute-intensive pipeline step; must be queued/rate-limited so a large evidence batch upload doesn't overwhelm the LLM API or the server.

## Acceptance Criteria
- Given a valid firewall-configuration screenshot matched against Requirement 1.2.1, a Finding is created citing the specific document and a confidence score.
- Given confidence 0.4, the resulting Finding has `needs_manual_review = true` regardless of suggested status.

## Explicitly Forbidden Behavior
The system must never present an AI-suggested status as if it were a final determination anywhere in the API response or UI — it must always be labeled as a suggestion pending human review.

---

# Feature: Finding Review (Accept / Edit / Reject)

## Purpose
The mandatory human-judgment checkpoint before anything counts toward the final report.

## Actors
Auditor (own assigned engagements), Reviewer (any engagement).

## Preconditions
Finding exists with `status = draft`.

## Trigger
Human reviews a Finding in the queue and takes an action.

## Inputs
`action` (enum: accept / edit / reject), `edited_status` (required if action=edit), `note` (optional free text, required if action=reject).

## Validation Rules
`edited_status` must be one of the same allowed enum values the AI could suggest.

## Processing Rules
- `accept`: `Finding.status = approved`, `final_status = ai_suggestion`, `reviewed_by`, `reviewed_at` set.
- `edit`: `Finding.status = approved`, `final_status = edited_status` (human value overrides AI value), reviewer fields set, original AI suggestion retained for audit trail (never overwritten).
- `reject`: `Finding.status = rejected`, note required, excluded from final report, but retained (not deleted) for audit trail.

## Business Rules
A rejected Finding is never deleted — it remains as a record of what the AI proposed and why a human disagreed, for future model-quality review.

## Authorization Rules
Auditors can only act on Findings within engagements they're assigned to. Reviewers can act on any Finding, including overriding an Auditor's prior accept/edit (with the override itself logged).

## Database Effects
Updates the `Finding` row; never deletes.

## External Dependencies
None.

## Success Output
`200 OK`, updated Finding object.

## Failure Cases
`edit` action without `edited_status` → `400`.
`reject` action without `note` → `400` (a rejection must be explainable).

## Edge Cases
Reviewer overrides an Auditor's earlier "accept" — both decisions are retained in an append-only history, current state reflects the Reviewer's action.

## Non-Functional Requirements
None beyond standard latency targets.

## Acceptance Criteria
- Given a draft Finding, when a Reviewer rejects it without a note, the response is 400 and the Finding remains `draft`.
- Given an Auditor accepts a Finding, when a Reviewer later edits the same Finding, the final state reflects the Reviewer's edit and both actions appear in the Finding's history.

## Explicitly Forbidden Behavior
The system must never allow a Finding to reach `approved` status without a `reviewed_by` user ID set.

---

# Feature: Engagement Finalization & Report Export

## Purpose
Produce the deliverable report and formally close the engagement — the one action that must always be a deliberate human act.

## Actors
Reviewer only.

## Preconditions
All `ScopedRequirement` rows have at least one linked Finding that is either `approved` or explicitly marked as a documented gap (auditor can finalize with known gaps, but never with unreviewed drafts).

## Trigger
Reviewer clicks "Finalize Engagement."

## Inputs
Engagement ID.

## Validation Rules
No `Finding` with `status = draft` may remain among the confirmed scope. Any confirmed requirement lacking an approved Finding must have an explicit `gap_acknowledged = true` flag set by the Reviewer.

## Processing Rules
1. Snapshot all `approved` Findings and acknowledged gaps into an immutable `Report` record.
2. Generate the export document (PDF) from the snapshot.
3. Set `Engagement.status = finalized`, `finalized_by`, `finalized_at`.

## Business Rules
Once finalized, an engagement's Findings become read-only. Any correction requires a new, explicitly-labeled addendum engagement or Finding — never a silent edit to a finalized record.

## Authorization Rules
Reviewer role required. Enforced server-side; the API must reject the action for any other role regardless of client-side UI state.

## Database Effects
Creates `Report` row (immutable snapshot); updates `Engagement.status`.

## External Dependencies
PDF generation (local/self-hosted).

## Success Output
`200 OK`, downloadable report reference.

## Failure Cases
Unresolved draft Findings exist → `409 Conflict`, response lists exactly which requirements are blocking finalization.

## Edge Cases
Reviewer wants to finalize with a known, acknowledged gap (e.g., client couldn't produce a specific artifact in time) — explicitly supported via `gap_acknowledged`, not a workaround.

## Non-Functional Requirements
Report generation must complete within 30 seconds for a typical engagement (~80 requirements); if it exceeds this, it must run as a background job with a completion notification rather than blocking the request.

## Acceptance Criteria
- Given 2 unresolved draft Findings, when finalize is attempted, the response is 409 listing those 2 items, and Engagement.status remains unchanged.
- Given all Findings approved or gaps acknowledged, when finalize is called by a Reviewer, Engagement.status becomes `finalized` and a Report is created.
- Given the same request made by an Auditor (non-Reviewer), the response is 403 regardless of Finding state.

## Explicitly Forbidden Behavior
The system must never auto-finalize an engagement on any schedule, timeout, or batch process. Finalization is exclusively a deliberate, single, human-initiated action.
