# 07_TASKS.md

Organized by dependency order. Complete each phase's tasks before starting the next, except where marked parallelizable. Every task follows the template below and must be executed one at a time by the coding agent.

**Critical path:** Phase 1 → 2 → 3 → 4 → 5 → 6 → 8 → 9 (Phase 7 integration tasks are embedded within Phase 5/6 where each pipeline step is actually needed, not a separate later phase, since the core features don't function without them).

---

## Phase 0 — Planning and Foundation

### TASK-001: Confirm open decisions
**Goal:** Resolve the `DECISION REQUIRED` items in 00_PRODUCT.md §5.8 before any code is written.
**Why It Exists:** Several decisions (product name, LLM provider/budget, evidence-request sending model) affect schema and config choices made in Phase 1–2.
**Dependencies:** None.
**Relevant Documentation:** 00_PRODUCT.md §5.8.
**Files/Layers Expected to Change:** None (documentation-only task).
**Requirements:** All `DECISION REQUIRED` items answered or explicitly deferred with a stated default.
**Implementation Constraints:** N/A.
**Security Requirements:** N/A.
**Tests Required:** N/A.
**Acceptance Criteria:** 00_PRODUCT.md updated to reflect confirmed decisions in place of `DECISION REQUIRED` markers.
**Explicitly Out of Scope:** Any code changes.
**Completion Checklist:** `[ ]` Decisions confirmed and documented.

---

## Phase 1 — Project Bootstrap

### TASK-002: Repository scaffolding
**Goal:** Set up the repository structure exactly as defined in 02_ARCHITECTURE.md §7.3.
**Dependencies:** TASK-001.
**Relevant Documentation:** 02_ARCHITECTURE.md §7.2, §7.3.
**Files/Layers Expected to Change:** New repo: `/backend`, `/frontend`, `/deploy`, `/docs`.
**Requirements:** FastAPI app boots with a `/health` endpoint; Next.js app boots with a placeholder page; both run via Docker Compose locally.
**Implementation Constraints:** Match the exact folder layout in 02_ARCHITECTURE.md §7.3 — do not invent an alternative structure.
**Security Requirements:** `.env.example` created, `.env` gitignored from the first commit.
**Tests Required:** A smoke test confirming `/health` returns 200.
**Acceptance Criteria:** `docker compose up` brings up backend + frontend + Postgres locally.
**Explicitly Out of Scope:** Any business feature code.
**Completion Checklist:** Per 06_ENGINEERING_RULES.md Definition of Done.

### TASK-003: CI baseline
**Goal:** Lint, type-check, and test run automatically on every commit/PR.
**Dependencies:** TASK-002.
**Relevant Documentation:** 06_ENGINEERING_RULES.md, 08_TESTING.md §CI Requirements.
**Requirements:** CI fails the build on lint error, type error, or failing test.
**Tests Required:** CI pipeline itself validated by an intentionally-failing test in a throwaway branch.
**Acceptance Criteria:** A PR with a lint violation fails CI.

---

## Phase 2 — Data Layer

### TASK-004: Core schema migration — User, Session
**Goal:** Implement the `User` entity and session table.
**Dependencies:** TASK-002.
**Relevant Documentation:** 03_DATA_MODEL.md → User.
**Files/Layers Expected to Change:** `/backend/app/models`, `/backend/migrations`.
**Security Requirements:** `password_hash` column, never a plaintext password column, even temporarily.
**Tests Required:** Migration applies cleanly up and down.
**Acceptance Criteria:** `User` table matches the field list in 03_DATA_MODEL.md exactly.

### TASK-005: Core schema migration — Engagement, EngagementAssignment
**Goal:** Implement Engagement and its assignment join table.
**Dependencies:** TASK-004.
**Relevant Documentation:** 03_DATA_MODEL.md → Engagement, EngagementAssignment.
**Tests Required:** Unique constraint on `(engagement_id, user_id)` verified.

### TASK-006: PCI DSS v4.0.1 corpus ingestion
**Goal:** Load the PCI DSS v4.0.1 requirement text into `PCIRequirement` rows, versioned.
**Dependencies:** TASK-002.
**Relevant Documentation:** 03_DATA_MODEL.md → PCIRequirement.
**Implementation Constraints:** Corpus text must be sourced from the actual published PCI DSS v4.0.1 standard (licensing/access terms for the standard itself must be checked before ingestion — this is outside the coding agent's authority to resolve and should be flagged `DECISION REQUIRED` if unclear at implementation time).
**Tests Required:** Row count matches expected clause count (~78 base requirements); spot-check a handful of clause IDs against the published standard.
**Acceptance Criteria:** Corpus is queryable by `clause_id` and `requirement_family`.
**Explicitly Out of Scope:** ISO/RBI/DPDP corpora (Stage 3).

### TASK-007: Remaining schema — ScopedRequirement, EvidenceRequest, EvidenceDocument, Finding, FindingHistory, Report
**Goal:** Implement the remaining entities from 03_DATA_MODEL.md.
**Dependencies:** TASK-005, TASK-006.
**Security Requirements:** `EvidenceDocument.storage_path` and `extracted_text` classified Sensitive per 03_DATA_MODEL.md §8.4 — confirm no default logging captures these (cross-check against 05_SECURITY.md §10.7).
**Tests Required:** Foreign key constraints verified; deletion-restriction behavior (03_DATA_MODEL.md §8.3) verified for at least one case (e.g., attempting to delete a User with existing `reviewed_by` Findings fails).

---

## Phase 3 — Authentication

### TASK-008: Login endpoint + session middleware
**Goal:** Implement POST /api/auth/login and session-resolution middleware.
**Dependencies:** TASK-004.
**Relevant Documentation:** 01_REQUIREMENTS.md → User Authentication; 04_API_CONTRACT.md → POST /api/auth/login; 05_SECURITY.md §10.2.
**Security Requirements:** Argon2id hashing, constant-time comparison, lockout logic, httpOnly/Secure/SameSite=Strict cookie.
**Tests Required:** Explicit tests for: valid login, invalid password, unknown email (identical response), lockout after 5 attempts, session cookie attributes.
**Acceptance Criteria:** Matches every acceptance criterion in 01_REQUIREMENTS.md → User Authentication.

### TASK-009: Seed script for initial Admin account
**Goal:** A one-time script to create the first Admin user (no self-registration exists).
**Dependencies:** TASK-008.
**Implementation Constraints:** Run manually, not exposed as an API endpoint.
**Security Requirements:** Script must not print the generated password to any persistent log.

---

## Phase 4 — Authorization

### TASK-010: Ownership-filtered repository layer
**Goal:** Implement the Engagement-scoped query pattern described in 03_DATA_MODEL.md §8.2 as a reusable repository base.
**Dependencies:** TASK-005, TASK-008.
**Relevant Documentation:** 02_ARCHITECTURE.md §7.4 (Repository layer rules); 03_DATA_MODEL.md §8.2; 05_SECURITY.md §10.3.
**Implementation Constraints:** Filtering happens in the SQL query itself (join against EngagementAssignment or role check), never "fetch all then filter in Python" — this is a hard rule, not a style preference.
**Tests Required:** A test proving a User not in `EngagementAssignment` for Engagement X gets 403/empty result when querying X's data, even when role=auditor.
**Acceptance Criteria:** This task is the single most important test target in the whole project — do not proceed to Phase 5 until this is solid.

---

## Phase 5 — Core Feature A: Engagement Creation & Scoping

### TASK-011: POST /api/engagements
**Dependencies:** TASK-010.
**Relevant Documentation:** 01_REQUIREMENTS.md → Engagement Creation; 04_API_CONTRACT.md → POST /api/engagements.
**Tests Required:** Per 01_REQUIREMENTS.md Acceptance Criteria for this feature.

### TASK-012: GET /api/engagements/{id} and list endpoint
**Dependencies:** TASK-011.
**Security Requirements:** Ownership filtering per TASK-010's pattern — no exceptions.

### TASK-013: LLM scope-suggestion service + POST /api/engagements/{id}/scope-suggestion
**Goal:** Implement the scoping service with the LLM call, timeout, and fallback behavior.
**Dependencies:** TASK-006, TASK-012.
**Relevant Documentation:** 01_REQUIREMENTS.md → PCI DSS Scope Matching; 02_ARCHITECTURE.md §7.6.
**Implementation Constraints:** 8-second timeout; graceful degradation to `manual_scoping_required: true` — must never return 500 for an LLM-unavailable case.
**Security Requirements:** Only structured profile fields sent to the LLM at this step — no evidence content (there is none yet at this stage, but confirm no accidental inclusion).
**Tests Required:** Test the timeout/fallback path explicitly (mock the LLM client to simulate a timeout).

### TASK-014: PATCH /api/scoped-requirements/{id}
**Dependencies:** TASK-013.
**Relevant Documentation:** 04_API_CONTRACT.md → PATCH /api/scoped-requirements/{id}.

---

## Phase 6 — Core Feature B: Evidence, Matching, Review, Finalization

### TASK-015: Evidence request generation
**Dependencies:** TASK-014.
**Relevant Documentation:** 01_REQUIREMENTS.md → Evidence Request Generation.
**Tests Required:** Verify no duplicate requests generated for already-satisfied requirements.

### TASK-016: Evidence document upload endpoint + storage
**Dependencies:** TASK-015.
**Relevant Documentation:** 01_REQUIREMENTS.md → Evidence Document Ingestion; 05_SECURITY.md §10.4, §10.5.
**Security Requirements:** Content-type inspection, size limit, filename sanitization, content-hash-addressed storage.
**Tests Required:** Reject a disguised executable; reject an oversized file; accept a valid PDF.

### TASK-017: Extraction pipeline (background worker)
**Dependencies:** TASK-016.
**Relevant Documentation:** 01_REQUIREMENTS.md → Evidence Document Ingestion (processing rules); 02_ARCHITECTURE.md §7.5, §7.6.
**Implementation Constraints:** Async, non-blocking on the upload request; sets `extraction_status` explicitly; stuck-in-`processing` sweep after a timeout.
**Tests Required:** Corrupt file → `extraction_failed`, not a crash; password-protected PDF → specific rejection.

### TASK-018: Embedding + retrieval pipeline
**Dependencies:** TASK-017, TASK-006.
**Relevant Documentation:** 01_REQUIREMENTS.md → Evidence-to-Clause Matching (steps 1–2); 02_ARCHITECTURE.md §7.6.
**Implementation Constraints:** Retrieval scoped only to the engagement's confirmed `ScopedRequirement` set, never the full corpus.

### TASK-019: Finding-generation LLM service
**Dependencies:** TASK-018.
**Relevant Documentation:** 01_REQUIREMENTS.md → Evidence-to-Clause Matching (steps 3–4).
**Implementation Constraints:** Every Finding created with `status=draft`; `needs_manual_review=true` if confidence < 0.6; LLM failure still creates a Finding (with nulls + manual-review flag), never silently drops it.
**Security Requirements:** Log call metadata only, never the evidence content sent to the LLM (05_SECURITY.md §10.7).
**Tests Required:** Confidence-threshold behavior; LLM-failure behavior; multi-clause-from-one-document case.

### TASK-020: Finding review endpoints
**Dependencies:** TASK-019, TASK-010.
**Relevant Documentation:** 01_REQUIREMENTS.md → Finding Review; 04_API_CONTRACT.md → PATCH /api/findings/{id}/review.
**Security Requirements:** `reviewed_by` always server-derived from session, never client-supplied. Reviewer-override case writes to FindingHistory, never silently overwrites.
**Tests Required:** Accept/edit/reject paths; Reviewer-overrides-Auditor case with history verification.

### TASK-021: Engagement finalization + report generation
**Dependencies:** TASK-020.
**Relevant Documentation:** 01_REQUIREMENTS.md → Engagement Finalization; 04_API_CONTRACT.md → POST /api/engagements/{id}/finalize.
**Security Requirements:** Reviewer-role check enforced server-side — this is the task 05_SECURITY.md §10.11 calls out as requiring an explicit dedicated test.
**Tests Required:** Unresolved-drafts blocks finalization with the correct list; non-Reviewer gets 403 regardless of UI state; already-finalized returns 409, not a duplicate Report.

---

## Phase 8 — Testing and Hardening

### TASK-022: Full authorization test suite pass
**Goal:** Systematic test coverage of every ownership/role boundary across all endpoints, not just the ones exercised incidentally by feature tests.
**Dependencies:** All of Phase 5–6.
**Relevant Documentation:** 08_TESTING.md → Security Tests.

### TASK-023: Dependency and secret scan
**Dependencies:** All prior tasks.
**Relevant Documentation:** 05_SECURITY.md §10.10, §10.11 checklist.

---

## Phase 9 — Deployment

### TASK-024: Docker Compose production config + Cloudflare Tunnel wiring
**Dependencies:** TASK-023.
**Relevant Documentation:** 09_DEPLOYMENT.md.

### TASK-025: First real engagement dry run
**Goal:** Run one actual client engagement through the full system end to end, with a human Reviewer genuinely finalizing it.
**Dependencies:** TASK-024.
**Relevant Documentation:** 00_PRODUCT.md §5.6 (Success Criteria).
**Acceptance Criteria:** This task's completion IS the Stage 1 exit condition referenced in the audit-copilot build sequence — a real, signed-off engagement, not a demo.

---

**Parallelizable:** TASK-006 (corpus ingestion) can run in parallel with TASK-004/005/008 (independent of User/Engagement schema). TASK-009 can happen any time after TASK-008.

**High-risk tasks:** TASK-010 (ownership filtering — gets this wrong and every later feature inherits the flaw), TASK-019 (Finding generation — the core "does the AI actually help" question), TASK-021 (finalization — the core "human stays in control" guarantee).
