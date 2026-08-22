# 08_TESTING.md

Testing priority is risk-based, not coverage-percentage-based. This system's actual risk concentration is: (1) cross-engagement data leakage, (2) the human-sign-off invariant, (3) the AI pipeline degrading gracefully rather than silently failing.

## Unit Tests

Required for:
- Scoping logic (which requirements get proposed for a given entity_type/merchant_level)
- Confidence-threshold logic (`needs_manual_review` flag setting)
- Finding state-machine transitions (draft → approved/rejected, and the override case)
- Finalization pre-condition check (unresolved drafts / gap_acknowledged logic)
- Password hashing and lockout-counter logic

## Integration Tests

Required for critical flows:
- Full auth flow: login → session cookie → authenticated request → logout/expiry
- Engagement creation → scope suggestion → confirmation → evidence request generation (the full happy path of Phase 5)
- Evidence upload → extraction → embedding → matching → Finding creation (the full happy path of Phase 6), including the LLM-failure and low-confidence branches
- Finalization: full path from "all findings approved" to "Report generated," and the blocked path with unresolved drafts

## Security Tests

Explicitly required (these map directly to 05_SECURITY.md §10.1's threat table):
- Unauthenticated access to any Engagement-scoped endpoint is rejected (401)
- An Auditor not in `EngagementAssignment` for Engagement X cannot read or write any of X's data (403), even by guessing/enumerating IDs
- A non-Reviewer calling POST /api/engagements/{id}/finalize is rejected (403) regardless of Finding state
- A Finding cannot reach `status=approved` without `reviewed_by` set — attempt this via direct service-layer call, not just via the API, to catch any bypass path
- Malicious file upload (disguised executable, oversized file, path-traversal filename) is rejected
- Login lockout triggers correctly and does not leak whether an email exists

## End-to-End Tests

Cover only the two most important user journeys end-to-end through the actual UI:
1. Journey 1–2 combined: create engagement → confirm scope → generate evidence checklist
2. Journey 3–4 combined: upload evidence → review a draft Finding → finalize an engagement

## Test Data Strategy

- **Fixtures:** a small, fixed set of PCI DSS v4.0.1 corpus rows (a handful of real clauses) for fast unit tests, separate from the full corpus load used in integration tests.
- **Factories:** factory helpers for User (per role), Engagement (per status), Finding (per state) to avoid repetitive setup code.
- **Isolation:** each test run against a fresh/transaction-rolled-back database — no shared mutable test state across tests.
- **Mocking boundaries:** the LLM API and embedding service are mocked in unit/integration tests by default; a small number of tests run against the real APIs (marked and run separately, not on every CI run, to control cost and flakiness).

## CI Requirements

Every PR must pass: lint, type-check, full unit test suite, integration test suite (against mocked external services). Real-API tests run on a schedule (e.g., nightly) or manually before a release, not on every commit.

## Coverage Priorities

1. Security boundaries (authorization, the finalization gate)
2. The human-sign-off invariant specifically (this is the product's core trust claim — treat it with the same rigor as a security boundary, not as an ordinary business rule)
3. Core business logic (scoping, matching, state transitions)
4. Data integrity (foreign key/deletion-restriction behavior)
5. The two end-to-end user journeys above

## Requirement-to-Test Mapping (critical items only)

| Requirement | Test |
|---|---|
| No Finding approved without `reviewed_by` | Unit test on the service layer directly + integration test via API |
| Finalize is Reviewer-only | Integration test, explicit 403 case |
| Ownership filtering on all Engagement-scoped data | Security test suite, TASK-022 |
| LLM timeout degrades gracefully, never 500s | Integration test with mocked timeout |
| Malicious file upload rejected | Security test, content-type spoofing case |
| No secrets in logs | A log-output test that scans for known secret patterns after a full request cycle |
