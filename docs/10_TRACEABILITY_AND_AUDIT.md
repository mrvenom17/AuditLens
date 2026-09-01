# REQUIREMENTS TRACEABILITY MATRIX

| Product Goal | Feature | Requirement | Architecture Component | Data Entity | API Endpoint | Security Control | Test | Task |
|---|---|---|---|---|---|---|---|---|
| Trustworthy automation, not just fast automation | Machine-Readable Control Definition | 01_REQ §Control Definition | control_corpus_service | ControlDefinition | POST/GET /control-definitions | Admin-only write; strict schema validation (05_SEC §10.4) | DETERMINISTIC-requires-rules test | TASK-102, 103 |
| Traceable evidence, not opinions | Fact Extraction | 01_REQ §Fact Extraction | fact_service | EvidenceFact | GET /audits/{id}/facts | No VERIFIED status without checkable location | Fact-with-no-value-not-fabricated test | TASK-104, 108 |
| Zero-LLM-dependency truth | Deterministic Rule Evaluation | 01_REQ §Deterministic Rule Evaluation | rule_engine.py | ControlEvaluation | POST /audits/{id}/evaluate | No API write path to `.result` (05_SEC §10.3) | Per-operator tests, LLM-unavailable test | TASK-105, 107 |
| Never trust an unverifiable claim | Evidence Gate | 01_REQ §Evidence Gate | evidence_gate.py | ControlEvaluation.gate_status | (internal, triggered by evaluate) | 10-point mechanical check, zero LLM | Fabricated-citation test, hash-mismatch test | TASK-109 |
| Human stays the decision-maker | Finding Review | 01_REQ §Finding Review | review_service | Finding, FindingHistory | PATCH /findings/{id}/review | system_result/auditor_decision structurally separate fields | Override-preserves-both-fields test | TASK-106, 114 |
| Resist manipulation | Adversarial & Safety Validation | 01_REQ §Adversarial & Safety Validation | fact_service, rule_engine, evidence_gate (structural) | ControlEvaluation | POST /audits/{id}/evaluate | 05_SEC §10.11 (5 tests) | The five AI Safety tests | TASK-113 |
| Never auto-finalize | Audit Finalization | 01_REQ §Audit Finalization | reporting_service | Report | POST /audits/{id}/finalize | Reviewer-only, immutable snapshot incl. engine/corpus version | 403-for-non-Reviewer test | TASK-115 |
| Prevent cross-client data leakage | (cross-cutting) | 05_SEC §10.3 | Repository ownership filter | All Audit-scoped entities | All Audit-scoped endpoints | Query-level filter (03_DATA §8.2) | Full authz suite | (existing, unchanged) |
| Secure account access | Authentication | 01_REQ §User Authentication | Auth middleware | User, Session | POST /auth/login | Argon2id, lockout, no enumeration | Lockout + enumeration tests | (existing, unchanged) |

---

# CONSISTENCY AUDIT RESULT

**Status: PASS WITH ASSUMPTIONS**

### Product ↔ Requirements
00_PRODUCT.md's Must-Have list and its acceptance-test table are both fully covered by 01_REQUIREMENTS.md's features, including the new Adversarial & Safety Validation feature, which exists specifically to make the acceptance table's rows testable requirements rather than aspirational claims.

### Requirements ↔ Architecture
Every requirement's "zero LLM dependency" claims (Deterministic Rule Evaluation, Evidence Gate) are backed by explicit architectural rules in 02_ARCHITECTURE.md §7.4 (MUST NOT import LLM client) — this is checked at two levels (documentation and, per 07_TASKS.md TASK-107, an actual import-boundary test), avoiding a documentation/code drift risk that would otherwise be the single biggest risk in this whole redesign.

### Architecture ↔ Data Model
`ControlEvaluation.result` having no API write path (02_ARCHITECTURE.md, 05_SECURITY.md §10.3) is reflected in 03_DATA_MODEL.md's Ownership Model and in 04_API_CONTRACT.md's endpoint definitions, none of which include a writable `result` field anywhere.

### Data Model ↔ Authorization
`EvidenceFact` and `ControlEvaluation` both inherit audit-assignment-based ownership per 03_DATA_MODEL.md §8.2, consistent with every other audit-scoped entity.

### API ↔ Requirements
Every new endpoint (control-definitions, facts, evaluate) traces to a feature in 01_REQUIREMENTS.md. The redefined `/findings` and `/findings/{id}/review` endpoints correctly expose `system_result` and `auditor_decision` as separate fields, matching 01_REQUIREMENTS.md → Finding Review's Explicitly Forbidden Behavior.

### API ↔ Security
The one security property hardest to get wrong in this whole system — no writable path to `ControlEvaluation.result` — is independently stated in 03_DATA_MODEL.md, 04_API_CONTRACT.md, 05_SECURITY.md §10.3, and 06_ENGINEERING_RULES.md. Redundant statement across four documents is intentional here, not documentation bloat, given how much of the product's trust claim rests on this one invariant.

### Security ↔ Testing
All five AI Safety tests in 05_SECURITY.md §10.11 have a corresponding row in 08_TESTING.md's AI Safety Tests table with a concrete setup and expected result — none are vague ("test for prompt injection") without a specific fixture.

### Tasks ↔ Requirements
07_TASKS.md is structured as a retrofit against an existing codebase (per the provided current-state assessment) rather than a from-scratch build — TASK-106 explicitly handles migrating existing Finding data rather than assuming a blank slate, which is a meaningful difference from how the original documentation revision was structured.

### Deployment ↔ Architecture
09_DEPLOYMENT.md correctly reflects that this redesign adds zero new infrastructure — the rule engine and Evidence Gate are in-process modules, consistent with 02_ARCHITECTURE.md's "no service decomposition, no new infra" framing.

## Issues Found
1. The provided roadmap and architecture documents use "Audit" terminology while the previously-generated documentation used "Engagement" — resolved by renaming throughout this revision, flagged explicitly in each file's header note rather than silently changed.
2. The provided roadmap assumes an existing codebase already implements much of the foundation; this documentation cannot verify that codebase's actual current field names/schema against what's specified here. `DECISION REQUIRED`: reconcile this documentation against the actual current schema before treating 03_DATA_MODEL.md as authoritative over the real database.
3. The 35-category master template (external input) was not built out as 35 separate files — resolved by explicitly scoping this revision to the existing 12-document structure and naming which categories (UI/UX spec, Risk Register, Compliance/Regulatory, Change-Control Governance, Master Constitution) are reasonable future additions rather than Level 0 requirements.

## Issues Resolved
All three above are resolved via explicit documentation (terminology note in each file; `DECISION REQUIRED` flag in 00_PRODUCT.md §5.8; explicit scoping statement in this response) rather than silent gaps.

## Outstanding Decisions
See 00_PRODUCT.md §5.8 (exact control list, malware-scan gating decision, schema reconciliation against any existing real codebase) — these require your confirmation or a direct look at the actual repository, not further documentation work, before Phase R0/TASK-101 can be marked complete.
