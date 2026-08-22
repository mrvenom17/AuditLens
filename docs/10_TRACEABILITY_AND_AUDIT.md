# REQUIREMENTS TRACEABILITY MATRIX

| Product Goal | Feature | Requirement | Architecture Component | Data Entity | API Endpoint | Security Control | Test | Task |
|---|---|---|---|---|---|---|---|---|
| Reduce scoping time | PCI Scope Matching | 01_REQ §Scope Matching | Service Layer + LLM (§7.6) | ScopedRequirement, PCIRequirement | POST /engagements/{id}/scope-suggestion | Rate limit; no client evidence sent at this step | Timeout-fallback test (TASK-013) | TASK-013 |
| Reduce evidence-chasing | Evidence Request Generation | 01_REQ §Evidence Request Generation | Service Layer | EvidenceRequest | POST /engagements/{id}/evidence-requests/generate | Draft-only, no external send (ADR-004) | No-duplicate-request test | TASK-015 |
| Speed up document review | Evidence Ingestion + Matching | 01_REQ §Evidence Ingestion, §Evidence-to-Clause Matching | Pipelines (extraction, embedding, LLM) | EvidenceDocument, Finding | POST /engagements/{id}/evidence-documents | Content-type inspection, size limit, no active-content execution | Malicious-upload test, confidence-threshold test | TASK-016, 017, 018, 019 |
| Never remove human judgment | Finding Review | 01_REQ §Finding Review | Service Layer state machine | Finding, FindingHistory | PATCH /findings/{id}/review | `reviewed_by` server-derived only (05_SEC §10.3) | Accept/edit/reject + override test | TASK-020 |
| Never auto-finalize | Engagement Finalization | 01_REQ §Finalization | Service Layer | Engagement, Report | POST /engagements/{id}/finalize | Reviewer-only role check (ADR-003) | 403-for-non-Reviewer test (explicitly called out, 05_SEC §10.11) | TASK-021 |
| Prevent cross-client data leakage | (cross-cutting) | 05_SEC §10.3 | Repository Layer ownership filter | All Engagement-scoped entities | All Engagement-scoped endpoints | Query-level ownership filter (03_DATA §8.2) | Full authz test suite | TASK-010, TASK-022 |
| Secure account access | Authentication | 01_REQ §User Authentication | Auth middleware | User, Session | POST /auth/login | Argon2id, lockout, no enumeration | Lockout + enumeration tests | TASK-008 |

---

# CONSISTENCY AUDIT RESULT

**Status: PASS WITH ASSUMPTIONS**

### Product ↔ Requirements
All V1 (POC) features in 00_PRODUCT.md §5.5 have a corresponding detailed requirement in 01_REQUIREMENTS.md. "Should Have" and "Nice to Have" items are intentionally not detailed in 01_REQUIREMENTS.md — consistent with POC scope.

### Requirements ↔ Architecture
Every requirement's external dependencies (LLM, embedding, extraction) are covered in 02_ARCHITECTURE.md §7.6 with timeout/retry/fallback behavior defined. No requirement assumes an architectural capability that isn't documented.

### Architecture ↔ Data Model
Every entity referenced in the architecture's data flow (§7.5) exists in 03_DATA_MODEL.md. The background-worker pattern in §7.5 matches the `extraction_status`/`processing` state fields in the EvidenceDocument entity.

### Data Model ↔ Authorization
Every user-owned/client-owned entity (Engagement and everything beneath it) has an explicit ownership rule in 03_DATA_MODEL.md §8.2, consistent with 05_SECURITY.md §10.3.

### API ↔ Requirements
Every endpoint in 04_API_CONTRACT.md traces to a feature in 01_REQUIREMENTS.md. No orphan endpoints; no requirement lacking an endpoint.

### API ↔ Security
Every protected endpoint states its authentication and authorization requirement explicitly. The one endpoint with the highest stakes (finalize) has an explicit, separately-called-out test requirement in both 05_SECURITY.md §10.11 and 08_TESTING.md.

### Security ↔ Testing
Every threat in 05_SECURITY.md §10.1's threat table has at least one corresponding test category in 08_TESTING.md's Security Tests section.

### Tasks ↔ Requirements
Every feature in 01_REQUIREMENTS.md has at least one task in 07_TASKS.md implementing it. TASK-010 (ownership filtering) is correctly identified as a prerequisite for all Phase 5–6 tasks rather than bundled into a single feature task, since it's a cross-cutting concern.

### Deployment ↔ Architecture
09_DEPLOYMENT.md's environment variables match every external dependency named in 02_ARCHITECTURE.md §7.2 and §7.6 (database, session secret, LLM key, embedding model path, file storage path).

## Issues Found
1. The original brief's "auto-generates a message" language was ambiguous between "drafts" and "sends" — resolved via ADR-004, documented as a Product/Architecture Challenge.
2. PCI DSS v4.0.1 standard text licensing/access terms for corpus ingestion (TASK-006) were not addressed in the original brief — this is a legal/procurement question outside what any document here can resolve unilaterally.

## Issues Resolved
Both of the above are resolved by explicit documentation (ADR-004; TASK-006's Implementation Constraints) rather than left as silent gaps.

## Outstanding Decisions
See 00_PRODUCT.md §5.8 and each `DECISION REQUIRED` marker across ADR-005 (external LLM data-handling disclosure to the firm) and ADR-007 (server isolation) — these require your confirmation, not further architectural work, before Phase 0/TASK-001 can be marked complete.
