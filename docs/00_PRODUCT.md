# 00_PRODUCT.md

> **Scope note:** this documentation set covers **Level 0 (Working PoC)** of AuditLens's 5-level roadmap: one framework (PCI DSS v4.0.1), **5–10 deterministically-verifiable controls only**, one test company, one auditor, one audit firm (single-tenant). Full-corpus scope, multi-tenant, continuous monitoring/connectors, and multi-framework support are Level 2+ and explicitly out of scope here. Terminology note: earlier drafts of this documentation used "Engagement" — this revision adopts **"Audit"** throughout to match the current codebase and target architecture.

## 5.1 Product Overview

- **Product name:** AuditLens
- **One-sentence description:** A deterministic-first, evidence-grounded audit support tool — GenAI drafts and explains, but every PASS/FAIL is produced by a rule engine running against provenanced facts, and a human auditor makes the final call.
- **Expanded description:** AuditLens ingests a client's evidence documents, extracts structured, source-traceable **Facts** from them (not opinions), evaluates those facts against machine-readable **Rules** attached to each control, and only then produces a system result — PASS, FAIL, PARTIAL, INSUFFICIENT_EVIDENCE, CONFLICT, or NOT_APPLICABLE. Every result must pass an **Evidence Gate** verifying its citation is real, current, and uncontradicted before it ever reaches a human. GenAI's role is bounded to drafting evidence-request language, explaining a system result in plain English, and drafting report prose — it never determines truth, never approves, and never modifies evidence or scope.
- **Core problem:** Prior-generation "AI compliance" tools ask an LLM to read evidence and judge compliance directly. This is fast but untrustworthy — the same mechanism that makes it fast (a language model inferring an answer) is the mechanism that makes it hallucinate, miss contradictions, or get manipulated by adversarial content embedded in evidence documents (prompt injection).
- **Why it matters:** An audit tool that occasionally hallucinates a PASS is worse than no tool — it doesn't just fail to save time, it actively creates false confidence in a compliance posture. Separating "what does the evidence literally say" (deterministic) from "how do I explain/communicate this" (GenAI) is what makes the speed gain trustworthy.
- **Proposed solution:** A single-tenant web application, one audit firm, PCI DSS v4.0.1 only, 5–10 controls selected specifically because they support deterministic verification (see §5.5).
- **Product boundaries:** No live connection to client cloud/IT systems (Level 4+ territory). No autonomous compliance verdicts — a system result is not a finding until a human reviews it. No multi-tenancy. No frameworks beyond PCI DSS v4.0.1.

## 5.2 Target Users

Unchanged from the prior revision: **Auditor**, **Reviewer**, **Admin** — see role matrix below. The one addition: Auditors and Reviewers now interact with a **System Result** (deterministic, or explicitly flagged as needing human judgment) rather than an "AI suggestion" — this is a meaningful trust distinction that should be visible in every screen that shows one.

## 5.3 User Roles

| Role | Permissions | Restricted actions | Data visibility | Ownership | Admin capabilities |
|---|---|---|---|---|---|
| Auditor | Create/edit assigned audits, upload evidence, trigger fact extraction/evaluation, edit draft findings | Cannot finalize audit; cannot delete evidence or facts; cannot override a REJECTED Evidence Gate result without Reviewer sign-off | Own assigned audits only | Owns audit drafts, not final sign-off | None |
| Reviewer | All Auditor actions + finalize/sign-off + override any Finding, including Evidence-Gate-rejected ones (with mandatory justification note) | Cannot delete evidence/facts (append-only) | All audits at the firm | Owns final sign-off | None |
| Admin | User management, control corpus management (including rule definitions), audit log access | Cannot finalize audits unless also a Reviewer; cannot edit a Control's rules on an audit already in progress (versioning applies — see 03_DATA_MODEL.md) | All audits (logged access) | System configuration | Full user/corpus management |

## 5.4 Core User Journeys

See 01_REQUIREMENTS.md for the fully detailed versions. At a high level, the flow is now:

```text
Create Audit → Select Company Profile → Scope Applicable Controls
→ Generate Evidence Requests (GenAI-drafted, human-sent)
→ Client Provides Evidence → Upload + Security Checks
→ Async: Extract → Chunk → Embed (discovery only) → Extract FACTS (with provenance)
→ Deterministic Rule Engine evaluates Facts against Rules
→ Evidence Gate verifies the result is citation-backed, current, uncontradicted
→ System Result produced (PASS/FAIL/PARTIAL/INSUFFICIENT_EVIDENCE/CONFLICT/NOT_APPLICABLE)
→ GenAI drafts a plain-language explanation of the System Result (never changes it)
→ Auditor/Reviewer reviews: Requirement + Exact Evidence + Facts + Rule + System Result + AI Explanation
→ Approve / Reject / Request More Evidence
→ Reviewer Finalizes → Immutable Report (snapshotting policy version, evidence hashes, rules, results, decisions)
```

## 5.5 Features

### Must Have — V1 (Level 0 PoC)
- Audit creation with company profile intake (from firm's existing records — no live client connectors)
- **5–10 hand-picked PCI DSS v4.0.1 controls, chosen specifically because they support deterministic evaluation** (e.g., minimum password length, MFA enabled, account lockout threshold, TLS minimum version, log retention period, encryption enabled, session timeout, password history, supported software version, required security logging)
- Machine-readable control definitions: `evaluation_mode`, `evidence_requirements`, `facts`, `rules` (see 03_DATA_MODEL.md)
- Evidence upload with hashing (SHA-256), MIME/content validation, and (flagged, not yet mandatory at Level 0) malware-scan hook
- Fact extraction with full provenance (document, page/line/cell, hash, timestamp, extractor version)
- Deterministic rule engine (`>=`, `<=`, `==`, `!=`, `IN`, `NOT_IN`, `CONTAINS`, `EXISTS`, `NOT_EXISTS`) with **zero LLM dependency**
- Evidence Gate — a hard checkpoint before any result reaches a human (see 01_REQUIREMENTS.md)
- Six-state System Result (not binary): PASS / FAIL / PARTIAL / INSUFFICIENT_EVIDENCE / CONFLICT / NOT_APPLICABLE
- GenAI used only for: evidence-request drafting, plain-language explanation of an already-determined System Result, report-prose drafting
- Human review queue distinguishing System Result (machine-determined) from Auditor Decision (human-determined) as genuinely separate fields
- Reviewer-only finalize + immutable report export
- Full audit trail: every fact, every rule evaluation, every gate check, every human decision

### Should Have
- Contradiction detection across multiple evidence documents for the same fact
- Evidence freshness/staleness rules per control
- Bulk evidence upload with auto-routing

### Nice to Have
- Direct in-app evidence-request sending
- A small golden-dataset regression suite (Level 1 territory, but worth prototyping early)

### Explicitly Out of Scope (this stage)
- Any control that genuinely requires human interpretive judgment rather than deterministic fact-checking (defer these to Level 1's "human-assisted" evaluation mode — don't fake determinism for a control that doesn't support it)
- Live connectors to client cloud/IAM systems (Level 4)
- Multi-tenancy, multiple frameworks, continuous monitoring
- Autonomous finalization of any kind

## 5.6 Success Criteria

Functional and technical criteria from the prior revision still apply. This revision adds the **PoC Acceptance Test Table** as a hard gate on calling Level 0 "done" — not optional polish:

| Test | Required System Behavior |
|---|---|
| Correct evidence provided | Result = PASS |
| Incorrect evidence provided | Result = FAIL |
| Evidence missing | Result = INSUFFICIENT_EVIDENCE |
| Two evidence sources conflict | Result = CONFLICT, routed to auditor, never silently resolved by the LLM |
| Evidence is stale (past a control's freshness window) | Result = STALE / REVIEW |
| A citation is fabricated (e.g., cites page 17 of a 5-page document) | REJECTED at the Evidence Gate — never reaches a Finding |
| Evidence document contains a prompt-injection payload instructing the system to mark compliant | No effect on the System Result — the rule engine has no path by which document *content* can alter its own evaluation logic |
| LLM/embedding API is unavailable | Deterministic controls still evaluate correctly — the rule engine has zero dependency on the LLM |
| Auditor rejects a PASS system result | Final report reflects the auditor's decision, not the system result |
| Control's rule definition is updated after an audit is finalized | The already-finalized report is unaffected (immutable snapshot) |
| An evidence file is altered after upload | Hash mismatch is detected |

A Level 0 PoC that cannot pass every row of this table is not done, regardless of how much of the happy path works.

## 5.7 Non-Goals

Same as before, with one addition: this product does not aim to make GenAI more accurate at judging compliance — it aims to remove GenAI from the judgment path entirely for anything that can be deterministically checked, and to make the human-assisted path (for genuinely interpretive controls) honest about needing a human, rather than dressed up as automation.

## 5.8 Assumptions and Open Questions

**Confirmed facts (from the provided roadmap/architecture):** deterministic-first evaluation is the target architecture; GenAI is explicitly non-authoritative; a modular monolith (FastAPI + PostgreSQL/pgvector + object storage + async workers + Next.js) is the correct Level 0 stack; 5–10 controls is the correct PoC scope; the six-state result model replaces binary PASS/FAIL.

**Reasonable assumptions:** "Audit" replaces "Engagement" as the primary entity name; the existing self-hosted deployment pattern (Ubuntu + Cloudflare Tunnel) still applies since nothing in the new architecture requires new infrastructure at Level 0.

**`DECISION REQUIRED` — all three now resolved during the retrofit:**
- ~~Which exact 5–10 controls~~ → **Resolved (ADR-011).** Eight controls: 8.3.6, 8.3.4, 8.3.7, 8.2.8, 8.4.2, 4.2.1, 10.5.1, 3.5.1. Authored as DETERMINISTIC in `backend/app/corpus/pci_dss_v4_0_1.json`; every other clause is HUMAN_ASSISTED. The set stops at eight rather than ten because the two next-best candidates check a cadence rather than a configured value, and force-fitting them would be exactly the dishonesty §5.7 rules out.
- ~~Malware scanning in or out for Level 0~~ → **Resolved (ADR-012).** Recorded, not upload-gating. `EvidenceDocument.malware_scan_status` defaults to `not_scanned` and is returned by the API, so the answer is visible rather than assumed.
- ~~Whether the existing codebase's schema differs from 03_DATA_MODEL.md~~ → **Resolved.** Reconciled in the codebase's favour where it had already made a choice, and in this document's favour where the retrofit introduced genuinely new structure. `Engagement`→`Audit`, `PCIRequirement`→`ControlDefinition` (with `clause_id`→`control_id`, `title`→`name`, `full_text`→`requirement_text`), and `ScopedRequirement`→`ScopedControl` were carried out as a real forward migration (`b1f2c3d4e5a6`), not by editing the released initial schema — an already-deployed database is renamed and its data preserved, including a data migration that maps each legacy `ai_suggested_status` onto a synthetic `ControlEvaluation` stamped `engine_version="legacy-llm-v0"`.
