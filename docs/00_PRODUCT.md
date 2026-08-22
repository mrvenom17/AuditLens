# 00_PRODUCT.md

> **Scope note:** this entire documentation set covers **Stage 1 (Deployed POC)** only, per the build sequence already agreed: one framework, one audit firm, no live client-infra connectors, human sign-off mandatory. Stages 2–5 (scaling, multi-framework, multi-tenant, business conversion) are explicitly future work and must not be implemented against this spec.

## 5.1 Product Overview

- **Product name:** AuditLens (confirmed 2026-08-22 — see ADR-008)
- **One-sentence description:** An internal tool that helps one audit firm's PCI DSS v4.0.1 assessment team scope requirements, request evidence, and draft control findings faster, with every finding requiring human review before it counts.
- **Expanded description:** AuditLens ingests a client's existing profile (already held by the firm, entered manually or uploaded — no live connection to client systems), determines which PCI DSS v4.0.1 requirements apply, tracks what evidence is missing, ingests client-submitted evidence documents, and produces AI-drafted findings (evidence → matched clause → suggested status → confidence score) for a human auditor to accept, edit, or reject. Nothing is final until a human marks it so.
- **Core problem:** Auditors spend the majority of engagement time on evidence-chasing and manual clause-matching rather than judgment-intensive analysis.
- **Why it matters:** This is repetitive, low-judgment work that scales linearly with engagement count — automating the first pass (not the decision) directly reduces billable-hour cost per engagement without reducing audit quality, if — and only if — the human remains the final authority.
- **Proposed solution:** A single-tenant web application used internally by one audit firm's engagement team, scoped to PCI DSS v4.0.1 only for this stage.
- **Product boundaries:** No live connection to any client's cloud/IT systems. No autonomous compliance verdicts. No multi-tenancy. No frameworks beyond PCI DSS v4.0.1.

## 5.2 Target Users

### User type: Auditor (Engagement Staff)
- **Who they are:** Junior-to-mid-level staff at the audit firm running the day-to-day engagement.
- **Goals:** Scope an engagement quickly, get evidence requests out fast, review AI-drafted findings efficiently.
- **Primary problems:** Manually re-reading the PCI DSS spec for every client, manually tracking what's missing, manually reading every submitted document to check against 78 base requirements.
- **Technical capability:** Comfortable with web apps and document upload; not expected to write code or understand the underlying AI/retrieval mechanics.
- **Allowed to:** Create/edit engagements they're assigned to, upload client profile info, generate evidence requests, upload evidence documents, review and edit AI-drafted findings for their own engagements.
- **Must never be allowed to:** Mark an engagement's final report as "signed off" (Reviewer-only action). Access engagements they aren't assigned to. Delete evidence documents once uploaded (audit-trail integrity).

### User type: Reviewer (Engagement Lead / Partner)
- **Who they are:** The senior/licensed professional accountable for the engagement's final output.
- **Goals:** Verify draft findings are correct, catch anything the AI or the junior auditor missed, produce the final client-facing report.
- **Allowed to:** Everything an Auditor can do, plus: override any finding on engagements they supervise, finalize and sign off an engagement, view all engagements at the firm.
- **Must never be allowed to:** Have the system auto-finalize a report without their explicit action.

### User type: Admin
- **Who they are:** Whoever manages the firm's use of the tool (likely you, initially).
- **Goals:** Manage user accounts, keep the PCI DSS corpus current, monitor system health.
- **Allowed to:** Create/deactivate user accounts, update the policy corpus, view audit logs.
- **Must never be allowed to:** Bypass the Reviewer-only sign-off step, even as Admin — sign-off authority is a role property, not an escalation path.

## 5.3 User Roles

| Role | Permissions | Restricted actions | Data visibility | Ownership | Admin capabilities |
|---|---|---|---|---|---|
| Auditor | Create/edit assigned engagements, upload docs, generate requests, edit draft findings | Cannot finalize engagement; cannot delete evidence; cannot see other auditors' unassigned engagements | Own assigned engagements only | Owns engagement drafts, not final sign-off | None |
| Reviewer | All Auditor actions + finalize/sign-off + override any finding | Cannot delete evidence (append-only for audit trail) | All engagements at the firm | Owns final sign-off | None |
| Admin | User management, corpus management, audit log access | Cannot finalize engagements unless also a Reviewer | All engagements (for support purposes) — access is logged | System configuration | Full user/corpus management |

## 5.4 Core User Journeys

### Journey 1: Scope a new engagement
```
Trigger: Auditor starts a new PCI DSS engagement for a client
→ User action: creates engagement, enters/uploads client profile (entity type, transaction volume tier, existing SAQ type if known, tech summary from firm's existing file)
→ System validation: required fields present (client name, entity type, merchant level)
→ Authentication check: valid session required
→ Authorization check: user has Auditor or Reviewer role
→ Processing: system matches client profile against PCI DSS v4.0.1 corpus, proposes applicable requirement set (e.g., SAQ D vs A-EP scope)
→ Database changes: Engagement record created; ScopedRequirement records created (proposed, editable)
→ External service interaction: LLM call for scope-matching suggestion (see 02_ARCHITECTURE.md §7.6)
→ Success response: engagement created with proposed scope, status = "scoping"
→ Failure scenarios: LLM call times out → engagement still created, scope left empty with a manual-scoping prompt (never blocks on the AI call)
```

### Journey 2: Generate and track evidence requests
```
Trigger: Auditor reviews the scoped requirement set
→ User action: requests "generate evidence checklist"
→ System validation: engagement must be in "scoping" or "in_progress" status
→ Authorization check: user assigned to this engagement
→ Processing: system compares scoped requirements against evidence already on file; drafts a checklist of missing items with plain-language descriptions
→ Database changes: EvidenceRequest records created, status = "drafted"
→ Success response: checklist shown to auditor for review/edit before the auditor sends it externally
→ Failure scenarios: no scoped requirements yet → error, must complete Journey 1 first
```

### Journey 3: Ingest and match evidence
```
Trigger: Auditor receives documents from the client (via their own channel) and uploads them
→ User action: uploads file(s) against one or more EvidenceRequest items
→ System validation: file type allowed (PDF, DOCX, XLSX, PNG/JPG), size limit enforced
→ Authorization check: user assigned to this engagement
→ Processing: text/structure extraction → embedding → retrieval against scoped clauses → LLM drafts a finding per matched clause with a confidence score and a citation to the specific clause and the specific evidence location
→ Database changes: EvidenceDocument record created; Finding record(s) created with status = "draft"
→ External service interaction: extraction pipeline, embedding model, LLM API
→ Success response: draft findings appear in the engagement's review queue
→ Failure scenarios: extraction fails (corrupt/unreadable file) → EvidenceDocument marked "extraction_failed", auditor notified, no Finding auto-created; low-confidence match → Finding still created but flagged `needs_manual_review = true`
```

### Journey 4: Review, override, and finalize
```
Trigger: All scoped requirements have at least one draft Finding (or auditor decides to proceed with gaps noted)
→ User action: Auditor/Reviewer works through the Finding queue — accept, edit, or override each
→ Authorization check: only Reviewer can change engagement status to "finalized"
→ Processing: report assembled from all Finding records with status = "approved" (draft/rejected findings excluded, gaps listed explicitly)
→ Database changes: Report record created, linked to the approved Finding set at time of generation (immutable snapshot)
→ Success response: Reviewer downloads/exports the report; engagement status = "finalized"
→ Failure scenarios: attempt to finalize with unresolved (still-draft) findings → blocked with an explicit list of what's unresolved; the system never finalizes on the auditor's behalf
```

## 5.5 Features

### Must Have — V1 (POC)
- Engagement creation with manual/uploaded client profile entry
- PCI DSS v4.0.1 corpus-backed scope suggestion
- Evidence-request checklist generation (draft only, human sends)
- Evidence document upload and extraction
- Evidence-to-clause matching with confidence score and citation
- Human review queue: accept / edit / reject each draft finding
- Reviewer-only finalize + report export
- Full audit trail of every AI suggestion and every human decision

### Should Have
- Bulk evidence upload with auto-routing to the right EvidenceRequest item
- Search across an engagement's evidence/findings
- Basic engagement dashboard (status, % of requirements with approved findings)

### Nice to Have
- Direct in-app email sending for evidence requests
- Prior-engagement evidence reuse for repeat clients

### Explicitly Out of Scope (this stage)
- Any framework other than PCI DSS v4.0.1
- Live connectors to client cloud/IAM/network systems
- Multi-tenancy (multiple audit firms)
- Autonomous pass/fail without human approval
- E-signature / legally-binding sign-off (sign-off in this tool means "marked finalized," not a legal attestation instrument)
- Billing/payments

## 5.6 Success Criteria

- **Functional:** one real client engagement runs end-to-end (scope → request → evidence → findings → finalize) inside the tool.
- **Technical:** system handles at least one full engagement's document set (typically 20–60 evidence artifacts for a mid-size merchant) without manual database intervention.
- **Reliability:** no data loss on LLM/extraction failures — failures degrade to "needs manual input," never to silent gaps.
- **Security:** no cross-engagement data leakage between auditors not assigned to the same engagement; verified by test (see 08_TESTING.md).
- **Deployment:** runs on existing self-hosted infra behind Cloudflare Tunnel with no new cloud accounts required.

## 5.7 Non-Goals

This product does not try to replace the QSA/auditor, does not try to be a general-purpose GRC platform, and does not try to serve more than one audit firm at this stage.

## 5.8 Assumptions and Open Questions

**Confirmed facts (from conversation):**
- Auditor-side only; audit-firm's own existing client records as the data source; human sign-off always required; efficiency (100%→30% workload) is the goal, not novelty; POC-first build sequence.

**Reasonable assumptions:**
- FastAPI + Next.js + PostgreSQL stack (matches existing skills/infra)
- Self-hosted deployment on existing Ubuntu/Cloudflare Tunnel infra
- Single audit firm, single-tenant for this stage

**Confirmed decisions (2026-08-22 — TASK-001 complete):**

| Open item | Resolution | Recorded in |
|---|---|---|
| Final product name | **AuditLens** | ADR-008 |
| Evidence requests sent vs drafted-only | **Drafted only.** The system never dispatches external communication. | ADR-004 (already accepted) |
| LLM provider | **Anthropic Claude**, accessed through the `LLMClient` abstraction in `/backend/app/pipelines/llm.py` so a provider change is a one-module edit. | ADR-009 |
| LLM budget ceiling | Enforced structurally rather than as a spend cap: scope-suggestion is rate-limited per user (10/hour, 04_API_CONTRACT.md), and the background matching queue is serialised. A hard currency ceiling is a provider-console setting, not an application concern. | ADR-009 |
| Embedding provider | **Self-hosted** `bge-small-en-v1.5` (384-dim) per ADR-005, loaded only in the worker process. | ADR-009 |
| PCI DSS v4.0.1 corpus text licensing | **Deferred, with a safe default.** The shipped corpus contains clause IDs, requirement families and titles with firm-authored summary text — not the copyrighted standard text. Replacing it with licensed text is a single-file swap. | ADR-010 |
| Stage-1 test-case engagement | Deferred to TASK-025 (a human activity, not an implementation input). | — |
| `403` vs `404` on `GET /api/engagements/{id}` | Keep them distinct, as 04_API_CONTRACT.md already specifies. Single-tenant internal software; existence-leakage to a firm employee is not a meaningful disclosure. | ADR-011 |
| Session concurrency (multiple active sessions per user) | Allowed, as 01_REQUIREMENTS.md already specifies. No single-session enforcement in POC. | — |
| Password reset | Admin-initiated only, no self-service flow, per 05_SECURITY.md §10.2. Implemented as the `reset-password` seed-script subcommand (TASK-009), not an API endpoint. | ADR-011 |
| Server isolation (ADR-007) | Deployment-time decision, does not block implementation. Compose file is written to run standalone on a dedicated host. | ADR-007 |
