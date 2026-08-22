# 03_DATA_MODEL.md

## Entity: User

**Purpose:** an account for firm staff.

**Fields:**
- `id` (UUID, PK)
- `email` (string, unique, not null)
- `password_hash` (string, not null, sensitivity: Secret)
- `name` (string, not null)
- `role` (enum: auditor / reviewer / admin, not null)
- `is_active` (boolean, default true)
- `created_at`, `updated_at` (timestamp)

**Primary Key:** `id`
**Foreign Keys:** none
**Relationships:** has many `Engagement` (via assignment), has many `Finding.reviewed_by`
**Unique Constraints:** `email`
**Indexes:** `email`
**Ownership Rules:** a User owns their own profile fields only; role changes require Admin action.
**Authorization Relevance:** `role` drives every permission check in the system.
**Lifecycle:** deactivated via `is_active = false`, never hard-deleted (preserves audit-trail integrity of past actions).
**Deletion Strategy:** soft delete only.

---

## Entity: Session

> Added by ADR-011. Required by TASK-004 and 01_REQUIREMENTS.md → User Authentication ("create a server-side session record").

**Purpose:** a server-side authenticated session. The browser holds an opaque random token in an httpOnly cookie; the database holds only its hash.

**Fields:**
- `id` (UUID, PK)
- `user_id` (FK → User.id, not null)
- `token_hash` (string, unique, not null, sensitivity: Secret — SHA-256 of the cookie token)
- `created_at` (timestamp, not null)
- `last_seen_at` (timestamp, not null — drives the 8-hour idle timeout)
- `absolute_expires_at` (timestamp, not null — `created_at + 24h`, never extended)
- `revoked_at` (timestamp, nullable — set on logout)

**Indexes:** `token_hash` (unique), `user_id`
**Ownership Rules:** a Session belongs to exactly one User and is never readable through any API.
**Authorization Relevance:** resolving a Session to a `(user_id, role)` pair is the first step of every authenticated request; role is re-read from the User row on each request, never cached in the session, so an Admin's role change or deactivation takes effect immediately.
**Lifecycle:** created on login; `last_seen_at` refreshed on each authenticated request; invalid once `revoked_at` is set, `last_seen_at` is older than 8 hours, or `absolute_expires_at` has passed.
**Deletion Strategy:** expired rows may be hard-deleted by a maintenance sweep — sessions are not audit records.

---

## Entity: LoginAttempt

> Added by ADR-011. Required by 01_REQUIREMENTS.md → User Authentication ("creates/updates a `LoginAttempt` counter on failure").

**Purpose:** the record backing the 5-attempts-per-15-minutes lockout.

**Fields:** `id` (UUID, PK), `email` (string, not null — recorded as submitted, lowercased; may not correspond to a real account), `succeeded` (boolean, not null), `created_at` (timestamp, not null)

**Indexes:** (`email`, `created_at`)
**Ownership Rules:** N/A — system table, never exposed through any API.
**Authorization Relevance:** lockout is evaluated **before** credentials are checked, and is keyed on the submitted email whether or not an account exists, so the lockout response cannot be used to enumerate accounts.
**Lifecycle:** append-only; rows older than the lockout window are irrelevant to the check.
**Deletion Strategy:** hard-deletable after 30 days — abuse-prevention state, not an audit record.

---

## Entity: ClientProfileDocument

> Added by ADR-011 item 6. Referenced by `source_document_ids` in 04_API_CONTRACT.md → POST /api/engagements and by 01_REQUIREMENTS.md → Engagement Creation, with no entity previously defined.

**Purpose:** a firm-internal document about a client (the existing client file the firm already holds), uploaded before or during engagement creation to seed the profile. Distinct from `EvidenceDocument`, which is client-submitted evidence attached to one engagement.

**Fields:**
- `id` (UUID, PK)
- `original_filename` (string, not null)
- `content_hash` (string, not null)
- `storage_path` (string, not null, sensitivity: Sensitive)
- `mime_type` (string, not null)
- `uploaded_by` (FK → User.id, not null)
- `created_at` (timestamp, not null)

**Ownership Rules:** firm-wide (single-tenant), readable by any authenticated Auditor/Reviewer/Admin. `source_document_ids` supplied at engagement creation are validated to exist, which is the "defensive check against ID-guessing" 04_API_CONTRACT.md describes — not a tenant boundary, since there is only one firm at this stage.
**Lifecycle:** immutable once created, append-only, consistent with `EvidenceDocument`.
**Deletion Strategy:** never hard-deleted.

---

## Entity: Engagement

**Purpose:** one PCI DSS v4.0.1 assessment for one client.

**Fields:**
- `id` (UUID, PK)
- `client_name` (string, not null, sensitivity: Internal)
- `entity_type` (enum: merchant / service_provider, not null)
- `merchant_level` (enum: 1/2/3/4, nullable — required if entity_type=merchant)
- `annual_transaction_volume` (integer, nullable)
- `existing_saq_type` (string, nullable — e.g. "A", "A-EP", "D"; added by ADR-011, accepted by POST /api/engagements)
- `tech_stack_summary` (text, nullable, sensitivity: Sensitive)
- `status` (enum: intake / scoping / in_progress / finalized, not null, default intake)
- `created_by` (FK → User.id)
- `finalized_by` (FK → User.id, nullable)
- `finalized_at` (timestamp, nullable)
- `created_at`, `updated_at`

**Primary Key:** `id`
**Foreign Keys:** `created_by → User.id`, `finalized_by → User.id`
**Relationships:** has many `ScopedRequirement`, `EvidenceRequest`, `EvidenceDocument`, `Finding`; has many assigned `User` via `EngagementAssignment`
**Unique Constraints:** none (a client may have multiple engagements over time)
**Indexes:** `status`, `created_by`
**Ownership Rules:** see Ownership Model below.
**Authorization Relevance:** central object — nearly every permission check traces back to "is this user assigned to this engagement, or a Reviewer."
**Lifecycle:** intake → scoping → in_progress → finalized (one-way; finalized is terminal for this engagement record).
**Deletion Strategy:** never hard-deleted once past `intake` with any evidence attached — this is a professional audit record.

---

## Entity: EngagementAssignment

**Purpose:** join table — which users are assigned to which engagement.

**Fields:** `id` (UUID, PK), `engagement_id` (FK), `user_id` (FK), `assigned_at` (timestamp)
**Unique Constraints:** (`engagement_id`, `user_id`)
**Ownership Rules:** only a Reviewer or Admin can add/remove assignments.

---

## Entity: PCIRequirement (corpus)

**Purpose:** the versioned PCI DSS v4.0.1 clause corpus — this is reference data, not client data.

**Fields:**
- `id` (UUID, PK)
- `clause_id` (string, e.g. "1.2.1", not null)
- `requirement_family` (integer 1–12, not null)
- `title` (string, not null)
- `full_text` (text, not null, sensitivity: Public — this is the published standard)
- `corpus_version` (string, e.g. "v4.0.1", not null)
- `effective_date` (date)
- `embedding` (vector, for retrieval)

**Indexes:** `clause_id`, `requirement_family`, vector index on `embedding`
**Ownership Rules:** N/A (firm-wide reference data, not per-client).
**Lifecycle:** versioned — a corpus update creates new rows tagged with the new `corpus_version` rather than mutating existing ones, so past engagements always cite the clause text that was actually in effect when they ran.

---

## Entity: ScopedRequirement

**Purpose:** which corpus clauses apply to a specific engagement.

**Fields:**
- `id` (UUID, PK)
- `engagement_id` (FK, not null)
- `pci_requirement_id` (FK → PCIRequirement.id, not null)
- `source` (enum: ai_suggested / manual, not null)
- `confirmed` (boolean, default false)
- `rationale` (text, nullable — the AI's or auditor's stated reason)
- `gap_acknowledged` (boolean, not null, default false — added by ADR-011; set only by a Reviewer, allows finalization of a confirmed requirement that has no approved Finding)
- `gap_note` (text, nullable — the Reviewer's stated reason for accepting the gap; required when `gap_acknowledged` is set true)
- `created_at`, `updated_at`

**Foreign Keys:** `engagement_id → Engagement.id`, `pci_requirement_id → PCIRequirement.id`
**Unique Constraints:** (`engagement_id`, `pci_requirement_id`)
**Ownership Rules:** editable only by users assigned to the parent engagement.
**Authorization Relevance:** `confirmed = true` is the gate that unlocks evidence-request generation for this clause.

---

## Entity: EvidenceRequest

**Purpose:** a drafted "please provide X" checklist item.

**Fields:**
- `id` (UUID, PK)
- `engagement_id` (FK, not null)
- `scoped_requirement_id` (FK, not null)
- `description` (text, not null)
- `status` (enum: draft / sent_externally / received, not null, default draft)
- `created_at`, `updated_at`

**Ownership Rules:** editable by users assigned to the engagement. `status = sent_externally` is set manually by the auditor as a note-to-self (the system does not verify actual sending — see Product Challenge #1).

---

## Entity: EvidenceDocument

**Purpose:** an uploaded file and its extracted content.

**Fields:**
- `id` (UUID, PK)
- `engagement_id` (FK, not null)
- `evidence_request_id` (FK, nullable — may be uploaded ad hoc, not against a specific request)
- `original_filename` (string, not null)
- `content_hash` (string, not null — content-addressed storage key)
- `storage_path` (string, not null, sensitivity: Sensitive)
- `mime_type` (string, not null)
- `extraction_status` (enum: processing / complete / extraction_failed, not null)
- `extracted_text` (text, nullable, sensitivity: Sensitive — client evidence content)
- `uploaded_by` (FK → User.id)
- `created_at`

**Ownership Rules:** immutable once created (no update/delete endpoint — append-only, per 01_REQUIREMENTS.md).
**Sensitivity:** `extracted_text` and `storage_path` are the two fields most likely to contain client-sensitive material — access must always be filtered through engagement assignment.

---

## Entity: Finding

**Purpose:** the AI-drafted, human-reviewed judgment on whether evidence satisfies a clause.

**Fields:**
- `id` (UUID, PK)
- `engagement_id` (FK, not null)
- `scoped_requirement_id` (FK, not null)
- `evidence_document_ids` (array of FK → EvidenceDocument.id — the documents cited, used for filtering)
- `citations` (JSON array of `{ evidence_document_id, location }` — added by ADR-011; `location` is a human-readable pointer such as `"page 3"` or `"sheet 'Firewall', row 12"`. 01_REQUIREMENTS.md → Evidence-to-Clause Matching rule 3 requires the citation to identify document **and** page/location, which a bare ID array cannot express. `evidence_document_ids` is derived from this field and kept in sync by the service layer.)
- `ai_suggested_status` (enum: satisfied / partial / not_satisfied / not_applicable, nullable — null if AI call failed)
- `ai_confidence` (float 0–1, nullable)
- `ai_rationale` (text, nullable)
- `needs_manual_review` (boolean, not null)
- `status` (enum: draft / approved / rejected, not null, default draft)
- `final_status` (enum, same values as ai_suggested_status, nullable until approved)
- `reviewed_by` (FK → User.id, nullable)
- `reviewed_at` (timestamp, nullable)
- `review_note` (text, nullable)
- `created_at`, `updated_at`

**Ownership Rules:** see Ownership Model below — this is the highest-stakes entity in the system.
**Lifecycle:** draft → approved (accept/edit) or draft → rejected. Terminal states are not further editable except by a Reviewer override, which is logged as a new history entry, not a silent overwrite.
**Deletion Strategy:** never deleted, including rejected Findings (audit trail of AI-quality over time).

---

## Entity: FindingHistory

**Purpose:** append-only log of every state change to a Finding, including Reviewer overrides of prior Auditor actions.

**Fields:** `id`, `finding_id` (FK), `actor_id` (FK → User.id), `action` (enum: accept/edit/reject/override), `previous_status`, `new_status`, `note`, `created_at`.
**Deletion Strategy:** never deleted — this table exists specifically so nothing is ever silently overwritten.

---

## Entity: Report

**Purpose:** the immutable, finalized snapshot handed to the client.

**Fields:** `id`, `engagement_id` (FK), `snapshot_data` (JSON — full copy of approved Findings + acknowledged gaps at finalize time), `generated_by` (FK → User.id), `generated_at`.
**Ownership Rules:** immutable once created.

---

## 8.1 Relationship Map

```text
User
 ├── creates → Engagement (created_by)
 ├── is assigned to → Engagement (via EngagementAssignment)
 └── reviews → Finding (reviewed_by)

Engagement
 ├── has many → ScopedRequirement
 ├── has many → EvidenceRequest
 ├── has many → EvidenceDocument
 ├── has many → Finding
 └── has one (when finalized) → Report

PCIRequirement (corpus, firm-wide reference data)
 └── referenced by → ScopedRequirement

ScopedRequirement
 ├── generates → EvidenceRequest
 └── has → Finding

EvidenceDocument
 └── cited by → Finding (many-to-many via evidence_document_ids)

Finding
 └── has many → FindingHistory
```

## 8.2 Ownership Model

**Who owns what?** An Engagement and everything under it (ScopedRequirement, EvidenceRequest, EvidenceDocument, Finding) is "owned" in the authorization sense by the set of Users in its `EngagementAssignment` list, plus any Reviewer at the firm.

**Who can read it?** Assigned Auditors: their assigned engagements only. Reviewers: all engagements. Admin: all engagements, but every Admin access to engagement content is logged distinctly from normal Reviewer access (Admins are not expected to routinely view client evidence).

**Who can modify it?** Assigned Auditors can modify ScopedRequirement, EvidenceRequest, EvidenceDocument (upload only, never edit/delete), and Finding (accept/edit/reject) within their assigned engagements. Reviewers can modify any of the above on any engagement, plus finalize.

**Who can delete it?** Nothing client-related is ever hard-deleted once an engagement leaves `intake` status. This is a deliberate business rule, not an oversight — these are professional audit records.

**How is ownership verified server-side?** Every repository method that fetches Engagement-scoped data takes the requesting `user_id` and role as parameters and applies the assignment filter (or Reviewer bypass) at the query level — never fetched unfiltered and checked afterward in application code. A resource ID in a URL is never sufficient authorization on its own: `GET /api/engagements/{id}/findings` always joins against `EngagementAssignment` (or checks `role=reviewer/admin`) in the query itself.

## 8.3 Data Integrity

- Foreign keys: `ON DELETE RESTRICT` for anything that would orphan an audit trail (e.g., you cannot delete a User who has `reviewed_by` records — deactivate instead).
- Transactions: Finding status transitions and their corresponding FindingHistory row are written in a single transaction — never one without the other.
- State transitions are enforced in the Service Layer (see 02_ARCHITECTURE.md §7.4), not just via database constraints, so the business rule ("no approved Finding without reviewed_by") is enforced before the row is ever written, not caught after the fact.

## 8.4 Sensitive Data Classification

| Classification | Fields |
|---|---|
| Public | `PCIRequirement.full_text` (published standard) |
| Internal | `Engagement.client_name`, `entity_type`, `merchant_level` |
| Sensitive | `Engagement.tech_stack_summary`, `EvidenceDocument.extracted_text`, `storage_path`, `Finding.ai_rationale` (may reference client-specific details) |
| Secret | `User.password_hash`, `Session.token_hash`, session cookie tokens, LLM/embedding API keys |

Sensitive-classified fields are never included in logs (see 02_ARCHITECTURE.md §7.8) and are only ever returned in API responses to users authorized against the parent Engagement.

## 8.5 Migration Strategy

Alembic migrations (consistent with existing Anagha workflow). Every migration is additive-first (add nullable column → backfill → make non-null in a later migration) to avoid the partial-application-loop failure pattern already documented from prior work on the self-hosted infra. No destructive migrations (dropping columns/tables) without an explicit, separately-reviewed migration.
