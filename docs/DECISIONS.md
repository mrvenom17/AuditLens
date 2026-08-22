# DECISIONS.md

# ADR-001: Monolithic architecture, not services

## Status
Accepted

## Context
Template guidance defaults toward questioning complexity; the product itself is a single-tenant tool for one firm at POC stage.

## Decision
Build as a single FastAPI backend + single Next.js frontend, no service decomposition.

## Reasoning
Scale (5–20 users, a handful of concurrent engagements) does not justify the operational overhead of multiple services. A monolith is faster to build, easier to reason about, and easier to deploy on existing self-hosted infra.

## Alternatives Considered
Microservices split by domain (auth, engagement, matching) — rejected as premature; nothing about the current scale or team size justifies the added deployment/coordination complexity.

## Consequences
Revisit only if Stage 4 (multi-tenant) load genuinely requires it — not before.

## Reversal Cost
Low-to-moderate at this stage (no external consumers of internal service boundaries yet); would grow significantly harder to reverse after Stage 4 multi-tenant customers exist.

## Date
2026-08-22

---

# ADR-002: Single-tenant for this stage

## Status
Accepted

## Context
The product's own build sequence (agreed prior to this documentation) explicitly defers multi-tenancy to Stage 4.

## Decision
No tenant-isolation data model, no per-tenant billing, no tenant-scoped auth — one firm, one deployment.

## Reasoning
Building multi-tenancy before a single real engagement has validated the core workflow would be solving a problem that doesn't exist yet at real cost to POC speed.

## Alternatives Considered
Building tenant_id into every table now "to save migration work later" — rejected; this is exactly the kind of invented-complexity the product's own stated goals (efficiency over novelty, POC-first) argue against.

## Consequences
A real migration effort will be needed at Stage 4 to add tenant isolation properly. This is accepted as the correct tradeoff.

## Reversal Cost
Moderate — adding a `firm_id`/tenant_id column and re-scoping every query is a real but well-understood migration, better done once with real Stage-4 requirements in hand than guessed at now.

## Date
2026-08-22

---

# ADR-003: No autonomous compliance verdicts — human sign-off is architecturally enforced

## Status
Accepted

## Context
Every current industry analysis of AI in audit/compliance tooling converges on the same point: the accountable professional must remain the final decision-maker. The user's own stated requirement independently confirms this.

## Decision
Enforce "no Finding reaches `approved` without `reviewed_by` set, and no Engagement reaches `finalized` except via a Reviewer-only, explicitly-triggered action" at the Service Layer and database level — not merely as a UI convention.

## Reasoning
A UI-only restriction can be bypassed by any future code path (a script, a batch job, a different frontend) that calls the service layer directly. Enforcing it at the layer that all callers must pass through makes the invariant structural, not aspirational.

## Alternatives Considered
Enforcing this only via frontend button visibility — rejected; this is precisely the kind of "trust the client" mistake 05_SECURITY.md explicitly warns against, applied to a business-critical invariant rather than just a security boundary.

## Consequences
Every new feature touching Findings or Engagement status must be checked against this invariant (06_ENGINEERING_RULES.md).

## Reversal Cost
This should never be reversed — it is not a technical convenience but the entire trust basis of the product, per the user's own explicit requirement.

## Date
2026-08-22

---

# ADR-004: Evidence requests are drafted, never sent, by the system

## Status
Accepted

## Context
The original brief's language ("generates a message asking for more documents") could be read as the system actively communicating with the client. This was flagged as a Product/Architecture Challenge.

## Decision
The system produces a draft checklist/message. Sending it to the client is a manual action the auditor performs through their own channel (email, portal, etc.), outside the system.

## Reasoning
Actually sending on the firm's behalf introduces deliverability, tone-control, and liability surface far beyond what a POC needs to prove, and it wasn't a validated requirement — it was an interpretation of ambiguous brief language.

## Alternatives Considered
In-app email sending — deferred, not rejected outright; genuinely useful later, listed as "Nice to Have" in 00_PRODUCT.md §5.5. `DECISION REQUIRED` if this should be pulled forward.

## Consequences
`EvidenceRequest.status = sent_externally` is a manual, unverified auditor note-to-self, not a system-confirmed delivery event. This is documented explicitly in 03_DATA_MODEL.md so no future developer assumes otherwise.

## Reversal Cost
Low — adding in-app sending later is additive, not a rework of existing data model or flows.

## Date
2026-08-22

---

# ADR-005: Self-hosted embeddings, external LLM API for reasoning

## Status
Accepted

## Context
Two different jobs exist in the pipeline: mechanical vectorization (embeddings) and actual reasoning/drafting (matching judgment, scope suggestions, request text).

## Decision
Run embeddings on a self-hosted small model; use an external LLM API for the reasoning-heavy steps.

## Reasoning
Embeddings at this volume are cheap to self-host and keeping them local avoids a recurring per-call cost and an additional round-trip of evidence content leaving the server for a purely mechanical step. Reasoning quality genuinely benefits from a frontier hosted model at this stage, and self-hosting an LLM of comparable quality is a materially larger undertaking than justified for a POC.

## Alternatives Considered
Fully self-hosted LLM — deferred, not rejected; revisit if per-call API cost or data-residency requirements (e.g., a client's contract terms) make it necessary later.

## Consequences
Client evidence content IS sent to an external LLM API during Finding generation — this must be disclosed to the audit firm and covered in whatever data-handling agreement exists between the firm and its own clients (this is a business/legal item, not something the coding agent can resolve — flagged `DECISION REQUIRED` for the user to confirm is acceptable before real client data is processed).

## Reversal Cost
Moderate — swapping the LLM call for a self-hosted model is isolated to the `/pipelines` module per the layer boundaries in 02_ARCHITECTURE.md, not a cross-cutting change.

## Date
2026-08-22

---

# ADR-006: FastAPI + Next.js + PostgreSQL stack

## Status
Accepted

## Context
The user already operates a FastAPI + Next.js + PostgreSQL/Alembic stack for an existing project (Anagha), with working self-hosted deployment experience on Ubuntu + Cloudflare Tunnels.

## Decision
Reuse the same stack for this project.

## Reasoning
Directly reuses existing skill, debugging experience, and deployment muscle memory — meaningfully de-risks the POC timeline versus learning a new stack simultaneously with building a new product.

## Alternatives Considered
A different stack chosen purely on technical merits in isolation — rejected; the marginal technical difference between reasonable modern stacks is small compared to the cost of context-switching for a solo POC build.

## Consequences
None significant — this is a low-risk, high-leverage decision.

## Reversal Cost
High (a full rewrite) — but there is no indication this stack is inadequate for the product's actual requirements, so this risk is theoretical.

## Date
2026-08-22

---

# ADR-007: Deploy on existing self-hosted infra, not a new cloud account

## Status
Proposed — `DECISION REQUIRED` on server isolation (see 09_DEPLOYMENT.md § Environments)

## Context
The user already runs a self-hosted Ubuntu + Cloudflare Tunnel setup hosting other applications.

## Decision
Deploy this application on the existing self-hosted infrastructure pattern, ideally on an isolated server/container rather than co-located with unrelated apps, given the sensitivity of client audit data.

## Reasoning
Avoids new cloud spend for a POC; reuses proven operational patterns (Cloudflare Tunnel, Docker Compose, Alembic migration workflow).

## Alternatives Considered
A managed cloud platform (e.g., a PaaS) — rejected for POC stage on cost grounds; worth reconsidering at Stage 4 (multi-tenant) when uptime/support expectations from paying customers change the calculus.

## Consequences
Operational responsibility (backups, patching, uptime) stays fully on the user, consistent with the existing pattern for Anagha/Pharmacare.

## Reversal Cost
Low-to-moderate — a Dockerized app can be redeployed to a managed platform later without a rewrite.

## Date
2026-08-22

---

# ADR-008: Product name is "AuditLens"

## Status
Accepted

## Context
00_PRODUCT.md §5.1 carried the name as a working title with a `DECISION REQUIRED` marker. TASK-001 required it resolved before Phase 1, because the name appears in the OpenAPI title, the frontend metadata, the Compose project name, and the production hostname.

## Decision
"AuditLens" is the confirmed product name for Stage 1.

## Reasoning
It was already the working title throughout the documentation set; no competing candidate was proposed, and continuing to defer would have blocked TASK-002 scaffolding for no benefit. The name is internal-only at this stage (one firm, no external users), so the cost of changing it later is a find-and-replace, not a rebrand.

## Alternatives Considered
Deferring until Stage 2 and scaffolding under a placeholder — rejected; a placeholder is a name, just a worse one that still propagates through the same files.

## Consequences
Appears in `pyproject.toml`, the FastAPI OpenAPI title, `frontend/package.json`, and the Compose project name.

## Reversal Cost
Trivial at Stage 1; grows only once an external hostname is published.

## Date
2026-08-22

---

# ADR-009: Anthropic Claude for reasoning, behind a provider-agnostic client interface

## Status
Accepted

## Context
02_ARCHITECTURE.md §7.2 names "LLM API (e.g., Claude)" without pinning a provider, and 00_PRODUCT.md §5.8 listed provider and budget ceiling as `DECISION REQUIRED`. Three features depend on it: scope suggestion (TASK-013), evidence-request drafting (TASK-015), and finding generation (TASK-019).

## Decision
Use Anthropic Claude via the official `anthropic` SDK, accessed exclusively through an `LLMClient` protocol defined in `/backend/app/pipelines/llm.py`. No route, service, or repository imports the vendor SDK directly. Budget control is structural (per-user rate limit on the interactive path, a serialised queue on the background path) rather than a currency ceiling in application code.

## Reasoning
The documentation already leaned toward Claude, and the three call sites are all reasoning-heavy tasks where a frontier hosted model is the documented choice (ADR-005). Wrapping it in a protocol costs about twenty lines and makes the swap ADR-005 anticipates ("revisit if per-call API cost or data-residency requirements make it necessary later") a one-module change rather than a cross-cutting one. A hard spend ceiling belongs in the provider console: an application-level cost counter would be a second source of truth that silently drifts from actual billing.

## Alternatives Considered
Raw `httpx` calls against a configurable base URL — rejected; it trades a well-maintained dependency for hand-rolled retry, error-taxonomy, and timeout handling on the exact code path 02_ARCHITECTURE.md §7.6 requires to be most robust. OpenAI — equivalent on merits, no reason to prefer it here.

## Consequences
`anthropic` is added to backend dependencies. `LLM_API_KEY` (already in the 09_DEPLOYMENT.md table) holds an Anthropic key. Every LLM call site must keep its documented fallback path, since the protocol makes failures uniform but does not make them impossible.

## Reversal Cost
Low — one module implements the protocol.

## Date
2026-08-22

---

# ADR-010: Corpus ships as a structural skeleton, not the copyrighted standard text

## Status
Accepted

## Context
TASK-006 requires PCI DSS v4.0.1 requirement text in `PCIRequirement` rows, and its own Implementation Constraints flag the standard's licensing terms as outside the coding agent's authority. The full text of PCI DSS v4.0.1 is copyrighted by the PCI Security Standards Council and distributed under terms that restrict redistribution.

## Decision
Ship `/backend/app/corpus/pci_dss_v4_0_1.json` containing, for each base requirement: `clause_id`, `requirement_family`, `title`, and a firm-authored plain-language summary in `full_text`. Do not ship, scrape, or reproduce the Council's text. The loader reads whatever is in that file; substituting a licensed full-text export is a single-file replacement requiring no code change.

## Reasoning
Every downstream component — embedding, vector retrieval, scope suggestion, evidence-request drafting, finding generation — depends only on the *shape* of a corpus row, not on the provenance of its `full_text`. Shipping a structurally complete corpus therefore unblocks TASK-006 through TASK-019 and makes them fully testable, while leaving the one genuinely legal question where it belongs: with the firm. The alternative — blocking the entire critical path on a procurement question — would have left nothing built and the question no closer to answered.

## Alternatives Considered
Ingesting the published text directly — rejected; the coding agent cannot verify the firm holds redistribution rights, and 06_ENGINEERING_RULES.md's precedence rules do not grant authority to resolve a legal question by assumption. Shipping an empty corpus — rejected; it blocks TASK-018/019 testing for no gain over a summary corpus.

## Granularity note
07_TASKS.md estimates "~78 base requirements". PCI DSS numbers clauses at two levels: base requirements (`x.y`) and the defined requirements beneath them (`x.y.z`). Evidence matches at the finer level — a firewall configuration satisfies `1.2.1`, not all of `1.2` — so rows are stored at `x.y.z`. The shipped corpus holds 205 defined requirements spanning 63 base requirements across all 12 families. Both counts are pinned by a test so a future corpus swap that changes granularity fails loudly instead of silently changing what "scope" means.

## Consequences
Retrieval and matching quality in the POC reflect summary text, not the literal standard. This is acceptable for validating the workflow (00_PRODUCT.md §5.6's functional criterion) but **must** be resolved before TASK-025 processes real client evidence, because a finding citing a paraphrase is not an audit-grade citation. `PCIRequirement.corpus_version` is set to `v4.0.1-summary` so no engagement can silently cite the skeleton as if it were the standard.

## Reversal Cost
Trivial by design — replace one JSON file, re-run the loader under a new `corpus_version`. Past engagements keep citing the version they actually ran against, per 03_DATA_MODEL.md's versioning rule.

## Date
2026-08-22

---

# ADR-011: Data-model additions required by higher-precedence documents

## Status
Accepted

## Context
The initial repository audit found six entities/fields that 01_REQUIREMENTS.md, 04_API_CONTRACT.md, or 07_TASKS.md require but that 03_DATA_MODEL.md does not define. Under the 06_ENGINEERING_RULES.md precedence order, 01_REQUIREMENTS.md and 05_SECURITY.md outrank 03_DATA_MODEL.md, so these are omissions in the data model rather than features to drop.

## Decision
Add to 03_DATA_MODEL.md:

1. **`Session`** — required by TASK-004 and 01_REQUIREMENTS.md §User Authentication ("create a server-side session record"). Fields: `id`, `user_id`, `token_hash`, `created_at`, `last_seen_at`, `absolute_expires_at`, `revoked_at`. The cookie carries a random opaque token; only its SHA-256 hash is stored, so a database read cannot mint a valid session.
2. **`LoginAttempt`** — required by 01_REQUIREMENTS.md §User Authentication ("creates/updates a `LoginAttempt` counter on failure"). Fields: `id`, `email`, `succeeded`, `created_at`. Lockout is a windowed count query, not a mutable counter row.
3. **`ScopedRequirement.gap_acknowledged`** (boolean, default false) plus `gap_note` — required by 01_REQUIREMENTS.md §Engagement Finalization and 04_API_CONTRACT.md's finalize endpoint. Without it the documented finalization validation rule is unimplementable.
4. **`Engagement.existing_saq_type`** (string, nullable) — accepted by `POST /api/engagements` in 04_API_CONTRACT.md and listed as an input in 01_REQUIREMENTS.md, but absent from the entity.
5. **`Finding.citations`** (JSON array of `{evidence_document_id, location}`) replacing a bare ID array — 01_REQUIREMENTS.md §Evidence-to-Clause Matching processing rule 3 requires "an explicit citation (document + page/location, clause ID)", which a plain FK array cannot express. `evidence_document_ids` is retained as a derived, indexed column for query filtering.
6. **`ClientProfileDocument`** — referenced by `source_document_ids` in 04_API_CONTRACT.md and by "Optionally links existing `ClientProfileDocument` rows" in 01_REQUIREMENTS.md, with no entity and no upload endpoint defined.

## Reasoning
Each item is a mechanical consequence of a higher-precedence document, not a new architectural decision, so resolving them inside the precedence rules is preferable to halting the build. They are recorded here rather than applied silently because 06_ENGINEERING_RULES.md requires documentation updates when implementation reveals a specification gap.

## Alternatives Considered
Dropping the affected features — rejected; each is mandated by 01_REQUIREMENTS.md, which outranks the data model. Inventing them silently in code — rejected explicitly by the engineering rules.

## Consequences
03_DATA_MODEL.md and 04_API_CONTRACT.md are updated in the same commit as this ADR. Item 6 additionally requires two endpoints that 04_API_CONTRACT.md does not define (`POST /api/client-profile-documents`, and assignment management) — see ADR-012.

## Reversal Cost
Low now, high after the first real engagement — these are schema changes to an append-only audit database.

## Date
2026-08-22

---

# ADR-012: Endpoints required by the data model but absent from the API contract

## Status
Accepted

## Context
03_DATA_MODEL.md states that "only a Reviewer or Admin can add/remove assignments" on `EngagementAssignment`, and 01_REQUIREMENTS.md requires `gap_acknowledged` to be "set by the Reviewer" — but 04_API_CONTRACT.md defines no endpoint for either. As written, an engagement could only ever be worked by its creator, and finalization with an acknowledged gap would be impossible to reach through the API.

## Decision
Add four endpoints, documented in 04_API_CONTRACT.md with full auth/authz rules:

- `POST /api/engagements/{id}/assignments` and `DELETE /api/engagements/{id}/assignments/{user_id}` — Reviewer/Admin only.
- `PATCH /api/scoped-requirements/{id}/gap` — Reviewer only, sets `gap_acknowledged` + `gap_note`.
- `POST /api/client-profile-documents` — firm-internal profile document upload, backing `source_document_ids` (ADR-011 item 6).

Plus the supporting endpoints the contract implies but omits: `POST /api/auth/logout`, `GET /api/auth/me`, `GET /api/engagements`, `GET /api/engagements/{id}/scoped-requirements`, `GET /api/engagements/{id}/evidence-requests`, `GET /api/engagements/{id}/evidence-documents`, `GET /api/engagements/{id}/report`, and the Admin user-management endpoints 00_PRODUCT.md §5.3 grants the Admin role.

## Reasoning
Each closes a gap where a documented capability has no reachable path. None introduces a new capability: every one implements a permission that 00_PRODUCT.md §5.3 or 03_DATA_MODEL.md §8.2 already grants. Omitting them would mean shipping roles whose documented powers cannot be exercised.

## Alternatives Considered
Auto-assigning all auditors to all engagements to avoid needing assignment endpoints — rejected outright; it would collapse the ownership boundary that 05_SECURITY.md §10.1 rates as the system's single Critical threat.

## Consequences
Every added endpoint is subject to the same ownership-filter and role rules as the contract's existing ones, and to TASK-022's authorization test sweep.

## Reversal Cost
Low.

## Date
2026-08-22

---

# ADR-013: Postgres as the background job queue; no message broker

## Status
Accepted

## Context
02_ARCHITECTURE.md §7.1 requires background workers in a separate process on the same host, and §7.5 describes the worker as picking up rows with `extraction_status=processing`. No broker (Redis, RabbitMQ, Celery) appears in the §7.2 technology stack or in 09_DEPLOYMENT.md's environment-variable table.

## Decision
The `evidence_documents` table is the queue. The worker polls for rows in a claimable state using `SELECT ... FOR UPDATE SKIP LOCKED`, processes them, and advances the status field. A sweep pass in the same loop marks rows stuck in `processing` past a timeout as failed, satisfying §7.5's stuck-row requirement.

## Reasoning
The architecture document already describes exactly this mechanism, so implementing it is adherence, not invention. `FOR UPDATE SKIP LOCKED` gives safe multi-worker claiming with no additional infrastructure, and at the documented scale (5–20 users, 20–60 documents per engagement) polling latency is irrelevant. Adding a broker would mean a new service to deploy, secure, back up, and monitor on a small self-hosted box — the operational overhead 02_ARCHITECTURE.md §7.1 and ADR-001 explicitly reject.

## Alternatives Considered
Celery + Redis — rejected as unjustified at this scale and absent from the documented stack. FastAPI `BackgroundTasks` — rejected; it runs in the API process, so a restart loses queued work and a large upload batch would compete with request handling, contradicting §7.9's reason for making the pipeline a background queue in the first place.

## Consequences
Job state is durable across worker restarts because it lives in the primary database. There is no retry-with-backoff scheduler beyond the documented single retry; if throughput ever becomes a real constraint this decision should be revisited before the worker is scaled horizontally.

## Reversal Cost
Low — the worker's claim/advance logic is confined to `/backend/app/pipelines/worker.py`.

## Date
2026-08-22
