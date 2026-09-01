# 07_TASKS.md

> **This is a retrofit plan, not a from-scratch build.** Per the current-state assessment: authentication, RBAC, the audit/scope model, evidence upload/storage, extraction, OCR, embeddings, RAG, LLM integration, human review, finalization, tests, CI, and deployment already exist as a working foundation. The remaining work is narrowly scoped to converting the evaluation core from "LLM judges compliance" to "deterministic rules judge compliance, LLM explains." Do not rebuild what already works.

**Critical path:** R1 → R2 → R3 → R4 → R5 → R6 → R7 → R8. This mirrors the Day 1–8 sequence in the source roadmap; phase boundaries here are dependency-based, not calendar-based.

---

## Phase R0 — Retrofit Planning

### TASK-101: Freeze the 5–10 control list
**Goal:** Select the specific PCI DSS v4.0.1 controls for Level 0, drawn from the actual implemented corpus (not the illustrative list in 00_PRODUCT.md §5.5).
**Dependencies:** None.
**Requirements:** Each selected control must genuinely support deterministic verification — reject any candidate that turns out to need interpretation once you look at its actual assessment procedure.
**Acceptance Criteria:** A written list of 5–10 `control_id`s with a one-line justification each for why it's deterministically verifiable.
**Explicitly Out of Scope:** Any control requiring `HUMAN_ASSISTED` mode — those are noted for Level 1, not built now.

---

## Phase R1 — Schema Additions

### TASK-102: ControlDefinition schema + migration
**Dependencies:** TASK-101.
**Relevant Documentation:** 03_DATA_MODEL.md → ControlDefinition; 01_REQUIREMENTS.md → Machine-Readable Control Definition.
**Implementation Constraints:** `evaluation_mode=DETERMINISTIC` requires non-empty `rules`/`facts`, enforced at the service layer AND the database layer (a check constraint or equivalent) — belt and suspenders on this one specifically.
**Tests Required:** Attempt to save a DETERMINISTIC control with empty rules → rejected.

### TASK-103: Author the frozen control set as ControlDefinition rows
**Dependencies:** TASK-101, TASK-102.
**Implementation Constraints:** Authored by a human (Admin action or seed script reviewed by a human) — never LLM-generated rules, per 01_REQUIREMENTS.md's Explicitly Forbidden Behavior.
**Acceptance Criteria:** All 5–10 controls exist as valid `ControlDefinition` rows, each passing TASK-102's validation.

### TASK-104: EvidenceFact schema + migration
**Dependencies:** TASK-102.
**Relevant Documentation:** 03_DATA_MODEL.md → EvidenceFact.
**Tests Required:** A fact cannot be marked `VERIFIED` without a non-null page/line/cell location (enforced at the service layer that creates these rows).

### TASK-105: ControlEvaluation schema + migration
**Dependencies:** TASK-104.
**Relevant Documentation:** 03_DATA_MODEL.md → ControlEvaluation.
**Implementation Constraints:** No API schema/route exposes a writable `result` field on this entity — verify this explicitly by checking that no Pydantic request model includes it.

### TASK-106: Redefine Finding to wrap ControlEvaluation
**Goal:** Migrate the existing Finding entity (currently likely storing an "ai_suggested_status" directly, per the prior architecture) to reference `ControlEvaluation` and carry a genuinely separate `auditor_decision` field.
**Dependencies:** TASK-105.
**Relevant Documentation:** 03_DATA_MODEL.md → Finding (Redefined).
**Implementation Constraints:** This is a real schema migration on existing data if any prior Findings exist — write a data-migration step that maps old `ai_suggested_status` values into a synthetic `ControlEvaluation` row per existing Finding (marked with a distinct `engine_version` like `"legacy-llm-v0"`) so historical data isn't silently lost, but is clearly distinguishable from genuinely deterministic evaluations going forward.
**Tests Required:** Existing Findings remain readable post-migration; new Findings correctly separate `system_result` from `auditor_decision`.

---

## Phase R2 — Rule Engine

### TASK-107: Build `rule_engine.py` as a pure, LLM-free module
**Dependencies:** TASK-104, TASK-105.
**Relevant Documentation:** 01_REQUIREMENTS.md → Deterministic Rule Evaluation; 02_ARCHITECTURE.md §7.4 (Rule Engine responsibilities); 06_ENGINEERING_RULES.md (Deterministic Core Invariant).
**Implementation Constraints:** Operators: `==, !=, >, >=, <, <=, IN, NOT_IN, CONTAINS, EXISTS, NOT_EXISTS`. Zero imports of the LLM/embedding client — enforce via an import-boundary test, not just review.
**Tests Required:** One test per operator; the four core acceptance scenarios (PASS/FAIL/INSUFFICIENT_EVIDENCE/CONFLICT) from 01_REQUIREMENTS.md's Acceptance Criteria; a test that runs the full engine with the LLM client mocked to raise `ConnectionError` on any call, proving zero dependency.

### TASK-108: Wire fact extraction to populate EvidenceFact for the frozen control set
**Goal:** Adapt the existing extraction/LLM-assist pipeline so it populates `EvidenceFact` rows (with provenance) instead of, or in addition to, whatever it currently feeds into finding generation.
**Dependencies:** TASK-104, TASK-103.
**Relevant Documentation:** 01_REQUIREMENTS.md → Fact Extraction.
**Implementation Constraints:** LLM assistance is scoped to "locate a candidate value's position in text" — the stored `EvidenceFact.value` and its location must be independently checkable by re-reading the cited location, not merely trusted from the LLM's output.
**Tests Required:** Given a fixture document with a clear value, a Fact is created with correct value/location; given a document with no discoverable value, no Fact is fabricated.

---

## Phase R3 — Evidence Gate

### TASK-109: Build `evidence_gate.py`
**Dependencies:** TASK-107, TASK-108.
**Relevant Documentation:** 01_REQUIREMENTS.md → Evidence Gate; 02_ARCHITECTURE.md (Evidence Gate must have zero external calls).
**Implementation Constraints:** All 10 checks are mechanical (hash comparison, page-count lookup, timestamp comparison) — no LLM call anywhere in this module.
**Tests Required:** One test per check category; specifically the fabricated-citation case (cite a page beyond document length) and the source_hash-mismatch case (alter a file after fact extraction, confirm detection).

---

## Phase R4 — Rescope GenAI Service

### TASK-110: Remove LLM authority from the existing finding-generation path
**Goal:** This is the actual "conversion" step — whatever currently calls the LLM to produce a compliance judgment must be changed to call the rule engine + evidence gate instead, with the LLM call moved to an explanation-drafting role on the resulting `ControlEvaluation`.
**Dependencies:** TASK-107, TASK-109, TASK-106.
**Relevant Documentation:** 02_ARCHITECTURE.md §7.4 (GenAI Service MUST/MUST NOT); 01_REQUIREMENTS.md → Finding Review.
**Implementation Constraints:** This is the single highest-risk task in the retrofit — search the existing codebase specifically for any place an LLM response is written into a field that determines compliance status, and redirect it. Do not leave a dead/parallel path where the old LLM-authoritative flow could still run for the frozen control set.
**Tests Required:** For each of the 5–10 frozen controls, confirm the only path to `ControlEvaluation.result` is via `rule_engine.py`, with a test that would fail if that path were bypassed.

### TASK-111: Evidence-request drafting and explanation drafting via GenAI (non-authoritative)
**Dependencies:** TASK-110.
**Relevant Documentation:** 01_REQUIREMENTS.md; 02_ARCHITECTURE.md §7.6 (three permitted LLM uses).
**Acceptance Criteria:** GenAI-drafted evidence-request text and `Finding.ai_explanation` populate correctly; disabling the LLM entirely still allows the Finding to display with raw facts/rule/result, just without prose.

---

## Phase R5 — Test Company & Fixtures

### TASK-112: Build the ACME-Payments-style test audit
**Dependencies:** TASK-103.
**Relevant Documentation:** 01_REQUIREMENTS.md → Audit Creation (`test_company` flag); 00_PRODUCT.md §5.6.
**Requirements:** Construct evidence documents (`password_config`, `iam_config`, `tls_config`, `logging_config` or equivalent, matching whatever the frozen 5–10 controls actually need) with deliberately varied outcomes: some PASS, some FAIL, one INSUFFICIENT (evidence deliberately omitted), one CONFLICT (two documents disagreeing).
**Acceptance Criteria:** Running the full pipeline against this fixture set produces exactly the expected result distribution, not just "something for each."

---

## Phase R6 — Adversarial Test Suite

### TASK-113: The five AI Safety tests
**Dependencies:** TASK-110, TASK-112.
**Relevant Documentation:** 05_SECURITY.md §10.11; 08_TESTING.md.
**Requirements:** Implement all five as automated tests, run in CI, not manual demos: prompt-injection ("evil test"), hallucination, fabricated citation, contradiction, LLM-unavailable.
**Acceptance Criteria:** All five pass. This task gates everything after it — do not proceed to R7 until it's green.

---

## Phase R7 — UI/Review Wiring

### TASK-114: Update the review queue UI to show system_result vs. auditor_decision distinctly
**Dependencies:** TASK-106, TASK-111.
**Relevant Documentation:** 04_API_CONTRACT.md → GET /api/audits/{id}/findings; 01_REQUIREMENTS.md → Finding Review.
**Implementation Constraints:** The UI must make `gate_status=REJECTED` visually unmistakable from a normally-verified result — this is a UX requirement with a security rationale, not a cosmetic one.

### TASK-115: Update report generation/export to include the full snapshot
**Dependencies:** TASK-114.
**Relevant Documentation:** 03_DATA_MODEL.md → Report.

---

## Phase R8 — Level 0 Acceptance

### TASK-116: Run the full PoC acceptance test table
**Dependencies:** All of R1–R7.
**Relevant Documentation:** 00_PRODUCT.md §5.6 (the eleven-row acceptance table).
**Acceptance Criteria:** Every row of the table passes as an automated test. This task's completion, not a demo, is what makes Level 0 done.

### TASK-117: First real (or realistic) audit run, human-finalized
**Dependencies:** TASK-116.
**Acceptance Criteria:** One audit — real or the constructed test company — runs fully through the system and is genuinely finalized by a human Reviewer, matching the original build sequence's Stage-1 exit condition.

---

**High-risk tasks:** TASK-110 (removing LLM authority from an existing, presumably-working path — easy to leave a bypass), TASK-107/TASK-109 (the two modules whose entire value is having zero LLM dependency — any accidental import breaks the core trust property), TASK-113 (the adversarial suite — this is where "looks done" and "is actually trustworthy" diverge).
