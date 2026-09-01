# 02_ARCHITECTURE.md

## 7.1 Architecture Overview

Still a single-tenant modular monolith — the new architecture adds internal modules (Fact Engine, Rule Engine, Evidence Gate) but does not change the deployment shape. Per the frozen target architecture: distributed workers, queues, Kubernetes, and service decomposition remain explicitly deferred until Level 3+, when there is an actual operational reason for them.

```text
Browser (Auditor / Reviewer / Admin)
   ↓ HTTPS via Cloudflare Tunnel
Next.js Frontend
   ↓
FastAPI Backend — Auth + Authorization middleware
   ↓
Domain Services: Audit Service, Scope Service, Evidence Service,
                  Fact Service, Evaluation Service, Gate Service,
                  Review Service, Reporting Service, Control Corpus Service
   ↓
Repository Layer (SQLAlchemy)
   ↓
PostgreSQL (+ pgvector) — system of record
   +
Object Storage (local volume at Level 0) — original evidence files, hash-addressed

Async worker (same host, separate process), triggered on upload:
   Extraction → OCR → Chunking → Embedding (discovery only)
                              ↘ Fact Extraction (with provenance) → Rule Engine → Evidence Gate
```

## 7.2 Technology Stack

Unchanged from the prior revision (FastAPI/Next.js/PostgreSQL+pgvector/SQLAlchemy+Alembic/Argon2id/self-hosted embeddings/external LLM API/Docker Compose) — see prior revision's table for rationale. **One addition:**

| Technology | Purpose | Why selected | Security implications |
|---|---|---|---|
| Deterministic rule engine (in-process Python module, not a third-party rules-engine library at this scale) | Evaluate `ControlDefinition.rules` against `EvidenceFact` rows | Simple enough (a handful of operators) that a dedicated rules-engine dependency is unjustified — write it as a small, heavily-tested internal module | This module has **zero network calls** by design — its testability and auditability are the whole point |

## 7.3 Repository Structure

```text
/backend
  /app
    /api
    /services
      /audit_service.py
      /scope_service.py         # applicability engine
      /evidence_service.py       # upload, storage, hashing
      /fact_service.py           # NEW — fact extraction orchestration
      /rule_engine.py            # NEW — pure, LLM-free deterministic evaluation
      /evidence_gate.py          # NEW — the gate checks
      /genai_service.py          # RENAMED/scoped — request drafting, explanations, report prose ONLY
      /review_service.py
      /reporting_service.py
      /control_corpus_service.py # NEW — versioned control definitions
    /repositories
    /models
    /schemas
    /pipelines                   # extraction, OCR, chunking, embedding (discovery only)
    /corpus                      # PCI DSS v4.0.1 control definitions, versioned
    /auth
    /config
  /migrations
  /tests
    /adversarial                 # NEW — prompt-injection, hallucination, fake-citation, contradiction tests
/frontend
  ...same as prior revision...
```

## 7.4 Layer Responsibilities

Prior revision's Route/Repository/Frontend rules still apply unchanged. **Service Layer rules are extended:**

### Rule Engine (`rule_engine.py`)
**MUST:** be pure/deterministic — same facts + same rules always produce the same result; be fully unit-testable with the LLM/embedding services entirely absent from the test environment.
**MUST NOT:** import or call any LLM/embedding client, under any circumstance, even for a "fallback" — if it cannot determine a result mechanically, it returns `INSUFFICIENT_EVIDENCE`, never delegates.

### GenAI Service (`genai_service.py`)
**MUST:** operate only on already-determined data (draft evidence-request text from a control's `evidence_requirements`; draft a plain-language explanation of an existing `ControlEvaluation.result`; draft report prose from an existing immutable snapshot).
**MUST NOT:** write to `ControlEvaluation.result`, `Finding.auditor_decision`, `ControlDefinition.rules`, or any field that determines compliance truth. Any code path where a GenAI service function's return value flows into one of those fields is a bug regardless of how it was introduced.

### Fact Service / Evidence Gate
**MUST:** treat all extracted document content as untrusted data, never as instructions — this is the concrete implementation of prompt-injection resistance (01_REQUIREMENTS.md → Adversarial & Safety Validation).
**MUST NOT:** allow a Fact's `verification_status` to be set to `VERIFIED` without a checkable source location.

## 7.5 Data Flow

**Evidence → System Result (the core flow, replacing the prior revision's "evidence → LLM finding" flow):**
```text
upload → hash + store → async worker:
  extract text/structure → OCR if needed → chunk
    ↓                                    ↓
  embed (pgvector, discovery-only)   fact_service: extract EvidenceFact rows
                                          (LLM may assist locating candidate values;
                                           stored value is data, not opinion)
                                          ↓
                                     rule_engine: evaluate ControlDefinition.rules
                                     against EvidenceFact rows → ControlEvaluation.result
                                          ↓
                                     evidence_gate: 10-point check → gate_status
                                          ↓
                                     (only if gate passes or is explicitly flagged UNCERTAIN)
                                     Finding created, review_service surfaces it
                                          ↓
                                     genai_service drafts explanation (display-only,
                                     never written back into system_result)
                                          ↓
                                     Auditor/Reviewer records auditor_decision
```

**Evidence Discovery (RAG) — demoted role:** pgvector answers "where might relevant evidence be" for the fact_service's search, and separately for an auditor manually browsing evidence — it never answers "is this compliant." No code path treats a vector-similarity score as a compliance signal.

## 7.6 External Services

Same LLM/embedding entries as the prior revision, with **scope restrictions tightened:**

### LLM API — now explicitly scoped to three non-authoritative uses
1. Scope suggestion (as before — proposing applicable controls from a company profile; advisory, human-confirmed)
2. Fact-location assistance (finding *where* a value appears in unstructured text — the value itself is then independently verifiable by a human against the citation)
3. Explanation/report drafting (rendering an already-determined result into prose)

**Never used for:** control rule authoring, direct compliance judgment, evidence-gate checks, resolving contradictions, or filling in a fact the extraction step couldn't find. Timeout/retry/fallback behavior per the prior revision's §7.6 still applies — and now, critically, **every one of these three uses has a defined behavior when the LLM is fully unavailable that still allows deterministic controls to evaluate correctly** (00_PRODUCT.md §5.6 acceptance test).

## 7.7 Error Handling Architecture

Unchanged structurally from the prior revision. One addition to the standardized error response vocabulary: `GATE_REJECTED` and `EVALUATION_INSUFFICIENT_EVIDENCE` join the existing error codes, used specifically so the frontend can render these states distinctly from a generic error.

## 7.8 Logging and Observability

Unchanged core rules (never log secrets/evidence content). **Additions specific to this architecture:**
- Log every `ControlEvaluation` creation with its `engine_version` and whether the LLM was involved at all in producing any input to it (should be "no" for DETERMINISTIC controls — this is a monitorable invariant, not just a design intent).
- Log every Evidence Gate check result, including which specific checks failed on a REJECTED/UNCERTAIN outcome — this is the data you'd want if you ever needed to demonstrate the system's trustworthiness to a skeptical auditor or regulator.
- Log every case where `auditor_decision != system_result` (override rate) — this is a genuine product-quality metric, not just an audit-trail entry.

## 7.9 Performance and Scaling

Unchanged from the prior revision. The rule engine and evidence gate add negligible load (in-process, no I/O beyond database reads already being made) — they are not a scaling concern at this or any near-term stage.
