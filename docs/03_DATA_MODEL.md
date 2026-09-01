# 03_DATA_MODEL.md

> Renamed: `Engagement`→`Audit`, `EngagementAssignment`→`AuditAssignment`, `ScopedRequirement`→`ScopedControl`, `PCIRequirement`→`ControlDefinition` (substantially richer). **New:** `EvidenceFact`, `ControlEvaluation`. **Redefined:** `Finding` now wraps a `ControlEvaluation` plus a genuinely separate `auditor_decision`, rather than storing an "AI suggestion" directly.

## Entity: User
Unchanged from the prior revision.

## Entity: Audit (was Engagement)
Same fields as the prior revision's Engagement, plus:
- `company_profile` (JSONB, default `{}`) — the structured profile the applicability engine evaluates conditions against: `industry`, `environment`, `systems[]`, `data_types[]`, `cloud_providers[]`, `stores_cardholder_data`, `transmits_cardholder_data`, `outsources_card_processing`. **An absent key means "not answered" and is not the same as an empty list**: the first yields UNDETERMINED, the second is a real negative answer.
- `test_company` (boolean, default false) — flags the fabricated ACME-Payments-style test audit used for the Level 0 acceptance tests (00_PRODUCT.md §5.6), keeping it structurally distinguishable from real client work everywhere (dashboards, reports, exports).

## Entity: AuditAssignment (was EngagementAssignment)
Unchanged in substance, renamed foreign key (`audit_id`).

## Entity: ControlDefinition (was PCIRequirement — substantially expanded)

**Purpose:** the machine-readable definition of one control — this entity is the foundation of the deterministic engine and did not exist in this form in the prior revision.

**Fields:**
- `id` (UUID, PK)
- `control_id` (string, e.g. "8.3.1", not null)
- `name` (string, not null)
- `requirement_text` (text, not null, sensitivity: Public)
- `requirement_family` (integer 1–12)
- `evaluation_mode` (enum: `DETERMINISTIC` / `STRUCTURED` / `HUMAN_ASSISTED`, not null)
- `evidence_requirements` (JSON array of `{type, description}`)
- `facts` (JSON array of `{name, type}` — the fact schema this control needs)
- `rules` (JSON array of `{fact, operator, expected}`, nullable if `evaluation_mode=HUMAN_ASSISTED`)
- `applicability_conditions` (JSON array of `{fact, operator, expected}` — evaluated against the Audit's `company_profile` at scoping time; empty means the control applies universally. `EXISTS`/`NOT_EXISTS` are rejected at authoring time because they cannot express UNDETERMINED)
- `assessment_procedures` (JSON array of strings — how an assessor tests this control, shown to the auditor beside the requirement)
- `freshness_window_days` (integer, nullable)
- `corpus_version` (string, not null)
- `embedding` (vector — used only for evidence discovery/RAG, never for evaluation)
- `superseded_by` (FK → ControlDefinition.id, nullable — versioning chain)

**Validation:** `evaluation_mode=DETERMINISTIC` requires non-empty `rules` and `facts` (01_REQUIREMENTS.md → Machine-Readable Control Definition). Enforced at the service layer at authoring time.
**Ownership Rules:** Admin-authored; firm-wide reference data.
**Lifecycle:** versioned via `superseded_by`, never mutated in place once any `ScopedControl` references it.
**Deletion Strategy:** never deleted once referenced by any Audit.

## Entity: ScopedControl (was ScopedRequirement)
Same structure as the prior revision's ScopedRequirement, FK renamed to `control_definition_id → ControlDefinition.id`, plus:
- `applicability_status` (enum: `IN_SCOPE` / `NOT_APPLICABLE` / `UNDETERMINED`, default `UNDETERMINED`)
- `applicability_evidence` (JSON, nullable — the conditions as evaluated, so an exclusion can be explained)
- `source` gains a third value, `deterministic`, distinguishing a rule's conclusion from a model's advisory proposal.

Rows are written only for controls that carry authored conditions, and only when the determination is informative — an `UNDETERMINED` row per unanswered question would bury the determinations that matter.

## Entity: EvidenceRequest
Unchanged from the prior revision.

## Entity: EvidenceDocument
Same as the prior revision, with fields renamed for consistency and one addition:
- `content_hash` → explicitly SHA-256 (stated, was left generic before)
- `malware_scan_status` (enum: `not_scanned` / `clean` / `flagged`, nullable — `DECISION REQUIRED` in 00_PRODUCT.md on whether this gates upload at Level 0 or is logged-only)

## Entity: EvidenceFact — NEW

**Purpose:** a structured, source-traceable claim extracted from evidence — the core new entity separating "a document exists" from "a checkable fact exists."

**Fields:**
- `id` (UUID, PK)
- `audit_id` (FK, not null)
- `control_definition_id` (FK, not null — which control this fact was extracted for)
- `name` (string, not null — matches a name in `ControlDefinition.facts`)
- `value` (string — stored as text, cast per `value_type` at evaluation time)
- `value_type` (enum: integer / boolean / string / date)
- `document_id` (FK → EvidenceDocument.id, not null)
- `page` / `line` / `cell` (nullable, at least one populated — the checkable location)
- `source_hash` (string, not null — the `EvidenceDocument.content_hash` at extraction time, for change detection)
- `observed_at` (timestamp — when the fact was true per the evidence, if statable)
- `extracted_at` (timestamp)
- `extractor_version` (string — which extraction/LLM-assist version produced this, for reproducibility)
- `verification_status` (enum: `VERIFIED` / `UNVERIFIED`, not null)

**Foreign Keys:** `audit_id → Audit.id`, `control_definition_id → ControlDefinition.id`, `document_id → EvidenceDocument.id`
**Indexes:** `(audit_id, control_definition_id, name)`
**Ownership Rules:** immutable once created (append-only, same principle as EvidenceDocument) — a superseding fact from newer evidence is a new row, not an edit.
**Authorization Relevance:** read access follows audit assignment.
**Lifecycle:** never deleted; a `source_hash` mismatch against the current document state (checked at evaluation/gate time) flags it, rather than removing it — the historical record of what was extracted, and when, is itself evidentiary.

## Entity: ControlEvaluation — NEW

**Purpose:** the mechanical, LLM-free result of running the rule engine — this is the entity that answers "what did the evidence, run through the rules, actually produce."

**Fields:**
- `id` (UUID, PK)
- `audit_id` (FK, not null)
- `control_definition_id` (FK, not null)
- `result` (enum: `PASS` / `FAIL` / `PARTIAL` / `INSUFFICIENT_EVIDENCE` / `CONFLICT` / `NOT_APPLICABLE`, not null)
- `evaluation_mode` (enum, copied from the control at evaluation time, not null)
- `facts_used` (array of FK → EvidenceFact.id)
- `rules_used` (JSON snapshot of the rules actually applied — so a later rule-definition edit can't retroactively change what this evaluation claims to have checked)
- `evidence_locations` (JSON — denormalized citation list for fast display)
- `contradictions` (JSON, nullable — populated when `result=CONFLICT`, listing the conflicting facts)
- `gate_status` (enum: `VERIFIED` / `UNCERTAIN` / `REJECTED`, not null)
- `gate_checks_failed` (array, empty if `gate_status=VERIFIED`)
- `evidence_strength` (enum: `STRONG` / `MODERATE` / `WEAK` / `NONE`) and `strength_factors` (array) — graded mechanically from verification status, corroboration across independent documents, freshness margin and citation granularity. Ordered gates, never a weighted score, and never model-derived.
- `evaluated_at` (timestamp)
- `engine_version` (string)

**Ownership Rules:** immutable once created — a re-evaluation (e.g., after new evidence arrives) creates a new `ControlEvaluation` row, never edits an existing one, preserving the history of what the engine said at each point in time.
**Authorization Relevance:** this entity's `result` field is the one piece of data in the entire system that must never be written by anything other than the rule engine (01_REQUIREMENTS.md → Deterministic Rule Evaluation, Explicitly Forbidden Behavior) — enforced by restricting write access to this column to the rule-engine service internally, not exposed on any API write path at all.

## Entity: Finding — REDEFINED

**Purpose:** the human-facing review record — now a genuinely separate wrapper around a `ControlEvaluation`, not a container for an "AI suggestion" as in the prior revision.

**Fields:**
- `id` (UUID, PK)
- `audit_id` (FK, not null)
- `control_evaluation_id` (FK → ControlEvaluation.id, not null)
- `ai_explanation` (text, nullable — GenAI-drafted plain-language rendering of the system_result; explicitly labeled as non-authoritative wherever displayed)
- `status` (enum: `pending_review` / `approved` / `rejected` / `needs_more_evidence`, not null)
- `auditor_decision` (enum, same value set as `ControlEvaluation.result`, nullable until reviewed — **genuinely distinct field from `ControlEvaluation.result`**, never overwrites it)
- `reviewed_by` (FK → User.id, nullable)
- `reviewed_at` (timestamp, nullable)
- `review_note` (text, nullable)

**Ownership Rules:** `auditor_decision`/`reviewed_by` writable only via the Finding Review feature's endpoint, by an assigned Auditor or any Reviewer.
**Lifecycle:** `pending_review` → `approved`/`rejected`/`needs_more_evidence`. An override (auditor_decision ≠ the wrapped ControlEvaluation.result) is always permitted and always logged via FindingHistory — human authority is final, but the disagreement is data.
**Deletion Strategy:** never deleted, matching the prior revision's rationale.

## Entity: FindingHistory
Unchanged from the prior revision.

## Entity: Report
Same as the prior revision, with `snapshot_data` now explicitly including, per control: `ControlEvaluation` (result, facts_used, rules_used, evidence_locations, gate_status) **and** `Finding.auditor_decision` as separate, both-preserved fields — plus `corpus_version` and `engine_version` at the top level, so a later change to either the control corpus or the rule engine can never retroactively alter what an already-finalized report claims.

---

## 8.1 Relationship Map

```text
User
 ├── creates / is assigned to → Audit
 └── reviews → Finding (reviewed_by)

Audit
 ├── has many → ScopedControl
 ├── has many → EvidenceRequest, EvidenceDocument
 ├── has many → EvidenceFact
 ├── has many → ControlEvaluation
 ├── has many → Finding
 └── has one (when finalized) → Report

ControlDefinition (firm-wide reference data, versioned)
 ├── referenced by → ScopedControl
 ├── declares facts required by → EvidenceFact
 └── declares rules evaluated into → ControlEvaluation

EvidenceDocument
 └── source of → EvidenceFact (with page/line/cell provenance)

EvidenceFact
 └── consumed by → ControlEvaluation (rule engine input)

ControlEvaluation
 └── wrapped by → Finding (adds auditor_decision, distinct from result)

Finding
 └── has many → FindingHistory
```

## 8.2 Ownership Model
Unchanged in structure from the prior revision (audit-assignment-based filtering at the query level). One addition specific to this architecture: **`ControlEvaluation.result` has no API write path at all** — it is not merely access-controlled, it is architecturally unreachable from any client request, written only by the internal rule-engine service. This is a stronger guarantee than a permission check and should be implemented as such (e.g., no Pydantic schema for creating/updating this field exists on any route).

## 8.3 Data Integrity
Same transactional principles as the prior revision. Addition: writing a `ControlEvaluation` and its `EvidenceGate` check result happen in one transaction — a `ControlEvaluation` row must never exist with a null `gate_status`, even transiently.

## 8.4 Sensitive Data Classification
Same table as the prior revision, with `EvidenceFact.value` and `ControlEvaluation.evidence_locations` added at **Sensitive** (they reveal specific client configuration values, same sensitivity tier as `EvidenceDocument.extracted_text`).

## 8.5 Migration Strategy
Same additive-first principle as the prior revision. Control corpus versioning (`ControlDefinition.superseded_by`) is the mechanism by which rule changes roll forward without migrating historical data — this is a data-modeling pattern, not a schema migration, and should be preferred over ad hoc "add a version column and hope" approaches.
