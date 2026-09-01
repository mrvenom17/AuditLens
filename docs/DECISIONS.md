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

# ADR-008: Deterministic-first evaluation; GenAI demoted to non-authoritative renderer

## Status
Accepted — supersedes the evaluation-mechanism portion of ADR-003 (ADR-003's core point, human sign-off is architecturally enforced, still stands and is reinforced by this decision, not replaced by it).

## Context
The prior architecture (documented in the original 01_REQUIREMENTS/02_ARCHITECTURE revision) had an LLM directly produce a suggested compliance status for each piece of evidence, gated only by human review afterward. A more rigorous target architecture was subsequently frozen, separating fact extraction, deterministic rule evaluation, and an Evidence Gate from GenAI, which is now restricted to evidence-request drafting, result explanation, and report prose.

## Decision
For any control marked `evaluation_mode=DETERMINISTIC` or `STRUCTURED`, the compliance result is produced exclusively by a rule engine with zero LLM/embedding dependency, checked by a mechanical Evidence Gate, before any human sees it. GenAI never determines truth.

## Reasoning
An LLM judging compliance directly is fast but fundamentally unauditable in the way that matters most for this domain: it can hallucinate a value, be manipulated by adversarial content in evidence (prompt injection), or silently resolve a genuine contradiction instead of surfacing it. Human review after the fact catches some of this but not reliably — reviewers anchor on a confident-sounding AI suggestion. Removing the LLM from the truth-determination path entirely, and making "I don't have enough evidence" a first-class correct answer, produces a system that is trustworthy by construction rather than by hoped-for reviewer vigilance.

## Alternatives Considered
Keeping the LLM-suggests/human-reviews model and hardening it with better prompting, confidence thresholds, and reviewer training — rejected; this treats a structural problem as a tuning problem, and the adversarial tests (05_SECURITY.md §10.11) specifically exist because prompting-level defenses are not reliable enough for this application's stakes.

## Consequences
Controls that don't genuinely support deterministic verification cannot be included in the Level 0 scope, however useful automating them would be — see TASK-101's explicit rejection criterion. This is a real scope constraint, not a implementation detail.

## Reversal Cost
High, and should not be reversed — this is the product's core trust claim, independently arrived at by both the roadmap analysis and the frozen target architecture. Any future "just let the AI decide when it's confident enough" proposal should be treated as a regression, not a feature.

## Date
2026-08-22

---

# ADR-009: RAG demoted to evidence-discovery only

## Status
Accepted

## Context
The prior architecture used vector retrieval as part of the pipeline that led directly to a compliance suggestion.

## Decision
pgvector/RAG answers only "where might relevant evidence be" — for the fact-extraction service's search and for an auditor manually browsing evidence. It never contributes to `ControlEvaluation.result`.

## Reasoning
A retrieval system's job is relevance ranking, not truth-verification — conflating the two is how a well-crafted but irrelevant document could previously have influenced a compliance judgment (RAG poisoning, 05_SECURITY.md §10.1).

## Alternatives Considered
Using retrieval confidence scores as an input to the rule engine — rejected; a similarity score is not a fact and has no place in a deterministic evaluation.

## Consequences
None negative — RAG remains fully useful for its actual job (helping fact-extraction and auditors find the right document faster).

## Reversal Cost
Low — this is a scoping decision about how a component's output is used, not a structural change to the component itself.

## Date
2026-08-22

---

# ADR-010: Level 0 scope narrowed to 5–10 deterministically-verifiable controls

## Status
Accepted — supersedes the earlier assumption (original 00_PRODUCT.md) that Level 0 would eventually cover a broader slice of the PCI DSS v4.0.1 corpus.

## Context
The frozen roadmap explicitly identifies "prove the complete architecture works end-to-end on a small, well-chosen control set" as the actual Level 0 bar, rather than breadth of coverage.

## Decision
Level 0's control corpus is limited to 5–10 controls, hand-selected specifically because their compliance status can be established from a fact plus a mechanical rule (e.g., minimum password length, MFA enabled, TLS minimum version) — not because they're the most commonly audited or most valuable controls in isolation.

## Reasoning
Proving the deterministic pipeline works correctly, including under adversarial conditions, on a small trusted set is more valuable at this stage than broad-but-shallow coverage of controls that would have to be force-fit into `evaluation_mode=DETERMINISTIC` without genuinely supporting it.

## Alternatives Considered
Scoping Level 0 to "as much of PCI DSS as the corpus already covers" (the original documentation's assumption) — rejected; this reintroduces exactly the temptation to fake determinism for controls that actually need human interpretation, which the new architecture's `HUMAN_ASSISTED` mode exists to honestly separate out instead.

## Consequences
Broader framework coverage is explicitly Level 1+ work (per the roadmap's own leveling), not a Level 0 deliverable.

## Reversal Cost
Low — expanding the control set later is additive (new `ControlDefinition` rows), not a rework of the pipeline itself.

## Date
2026-08-22

---

# ADR-011: The frozen Level 0 control set is these eight

## Status
Accepted — resolves the first `DECISION REQUIRED` in 00_PRODUCT.md §5.8 and completes TASK-101.

## Context
ADR-010 fixed the *size* of the Level 0 set (5–10) and its selection principle, but the actual control ids had to be drawn from the implemented corpus rather than from the illustrative examples in 00_PRODUCT.md §5.5.

## Decision
Eight controls, authored as `evaluation_mode=DETERMINISTIC` in `backend/app/corpus/pci_dss_v4_0_1.json`:

| Control | Fact | Rule | Why it is deterministically verifiable |
|---|---|---|---|
| 8.3.6 | `minimum_password_length` | `>= 12` | A single integer in a password-policy export. |
| 8.3.4 | `account_lockout_threshold` | `<= 10` | A single integer; the standard caps it at 10 attempts. |
| 8.3.7 | `password_history_count` | `>= 4` | A single integer; the standard requires the last four. |
| 8.2.8 | `idle_session_timeout_minutes` | `<= 15` | A single integer in minutes. |
| 8.4.2 | `mfa_enabled` | `== true` | A boolean in an identity-provider export. |
| 4.2.1 | `tls_minimum_version` | `IN ["1.2", "1.3"]` | An enumerated string in a TLS/proxy config. |
| 10.5.1 | `log_retention_months` | `>= 12` | A single integer in months. |
| 3.5.1 | `pan_rendered_unreadable` | `== true` | A boolean in a storage-encryption config. |

Every other clause in the 205-clause corpus is authored `HUMAN_ASSISTED` and is never routed through the rule engine.

## Reasoning
Each of these reduces to one value a human can confirm by opening the cited document at the cited page. That is the actual test of deterministic verifiability — not whether the control is important, but whether "what does the evidence literally say" has a single mechanical answer.

## Alternatives Considered
Including 8.3.9 (90-day password rotation) and 1.2.7 (six-monthly NSC review) to reach ten — rejected. Both depend on comparing a *cadence* against a review history rather than reading a configured value, which is closer to an interpretive judgment than a fact lookup. 06_ENGINEERING_RULES.md § Scope Control explicitly warns against force-fitting a control into DETERMINISTIC to reach a round number, so the set stops at eight.

## Consequences
The rules are human-authored in a version-controlled file, reviewed as code. An LLM has no path to them (01_REQUIREMENTS.md). Expanding the set is additive: new rows, no pipeline change.

## Reversal Cost
Low — the set is data, not code.

## Date
2026-09-01

---

# ADR-012: Malware scanning is recorded, not upload-gating, at Level 0

## Status
Accepted — resolves the second `DECISION REQUIRED` in 00_PRODUCT.md §5.8.

## Context
The target architecture flags malware scanning as "mandatory for higher-assurance deployment" without saying whether Level 0 must gate uploads on it. The answer changes TASK sequencing, because gating requires a scanner in the deployment before evidence upload can work at all.

## Decision
`EvidenceDocument.malware_scan_status` exists and is returned by the API, defaulting to `not_scanned`. Upload is **not** blocked on it at Level 0.

## Reasoning
The existing upload path already enforces the controls that matter against the actual Level 0 threat: size limits, MIME/magic-byte validation, extension/content agreement, and passive-only parsers that never execute embedded content (01_REQUIREMENTS.md § Evidence Ingestion). Level 0 runs against a fabricated test company and, at most, one consenting client's documents on a single-tenant self-hosted box. Introducing a scanner dependency now would add a deployment component and a failure mode to the upload path without addressing a threat that is live at this stage.

Making the field exist but read `not_scanned` is the honest option: the answer to "was this scanned?" is visible in the record rather than assumed either way.

## Alternatives Considered
- **Gate uploads on a clean scan now** — rejected as premature for Level 0; it adds an operational dependency ahead of the threat.
- **Omit the field entirely until it is enforced** — rejected; a report that cannot say whether evidence was scanned is worse than one that says plainly that it was not.

## Consequences
Before any deployment handling real client evidence at volume, this becomes a gating check — the column and the API field are already in place, so that change is a service-layer edit, not a migration.

## Reversal Cost
Very low — the schema and contract already carry the field.

## Date
2026-09-01

---

# ADR-013: Applicability is deterministic; the LLM is demoted to advisory

## Status
Accepted.

## Context
The target architecture says the control corpus carries applicability conditions and the Scope Engine uses them to decide which controls apply. The code did neither: `ControlDefinition` had no conditions column, and scope was decided entirely by asking an LLM to pick clause ids from five profile fields.

The consequence was worse than a missing feature. Nothing anywhere passed `applicable=False` to the rule engine, so `EvaluationResult.NOT_APPLICABLE` — one of the six documented result states — could never be produced. The system could describe the state and not reach it.

## Decision
`ControlDefinition.applicability_conditions` holds `[{fact, operator, expected}]`, AND-combined, evaluated against a structured `Audit.company_profile`. Scoping runs the deterministic pass first; the LLM then proposes over what the engine left open, and any proposal for a control the engine excluded is dropped and logged. `EvaluationService` passes `applicable=` so the sixth result state is reachable.

The engine reuses `rule_engine.evaluate` via a small adapter rather than reimplementing comparison — a second operator implementation would be free to drift from the tested one.

## Reasoning
Applicability is a mechanical question ("is this entity a service provider?"), and mechanical questions belong on the deterministic side of this architecture for the same reason compliance verdicts do. Keeping the LLM as an advisory pass preserves its genuine value — proposing controls nobody authored a condition for — without letting it decide.

## Alternatives Considered
- **Deterministic only, drop LLM scoping** — rejected; 167 of 205 controls carry no conditions, and they would never be proposed at all.
- **Keep the LLM primary, add conditions as display metadata** — rejected; the Scope Engine would still not be what the architecture describes.

## Consequences
`UNDETERMINED` is a first-class state and must never collapse into `NOT_APPLICABLE`. Three defences enforce that: an absent profile key emits no fact (so conditions on it resolve INSUFFICIENT_EVIDENCE → UNDETERMINED); an answered-but-empty list emits explicit negatives and *is* allowed to exclude; and `EXISTS`/`NOT_EXISTS` are rejected at authoring time because they answer PASS/FAIL for a missing fact and could therefore turn silence into exclusion.

The Evidence Gate had to learn about this too — a NOT_APPLICABLE control has no evidence, and the gate's no-facts branch would otherwise have flagged every correctly-excluded control as unverifiable.

## Reversal Cost
Low. Conditions are data; removing them returns scoping to LLM-only.

## Date
2026-09-01

---

# ADR-014: Evidence strength is a deterministic rubric, not a score

## Status
Accepted.

## Context
The architecture shows the auditor "evidence strength" during review. Nothing computed it. The nearest signals were binary (`VerificationStatus`) or three-state (`GateStatus`), which say whether evidence is usable, not how much weight it bears.

## Decision
`evidence_strength.assess()` grades STRONG/MODERATE/WEAK/NONE from provenance already held: verification status, corroboration across **independent documents**, freshness margin (≥50% of the control's window), citation granularity, gate outcome and contradictions. `strength_factors` records which criteria fired.

## Reasoning
Ordered gates rather than a weighted score, because a threshold on a weighted sum cannot be explained to an auditor — and an unexplainable grade is no better than the model opinion this architecture removed. Corroboration counts distinct `document_id`s specifically: two extractions of one value from one export are one observation, and counting them twice would let a repetitive config dump grade STRONG.

## Alternatives Considered
- **LLM-scored strength** — rejected outright; it would reintroduce model judgment about how far to trust evidence, which is the auditor's job.
- **Auditor-assigned** — rejected as the default; it adds review effort and yields no automatic signal, though an auditor can still override the result it informs.

## Consequences
A control with no `freshness_window_days` can never reach STRONG, since there is no margin to sit inside. All eight Level 0 deterministic controls declare a window, so this costs nothing today; the factor `no_freshness_window` makes the ceiling visible rather than mysterious.

## Reversal Cost
Low — one pure module and one column.

## Date
2026-09-01

---

# ADR-015: STRUCTURED mode checks presence and shape, not values

## Status
Accepted.

## Context
`EvaluationMode` declared three members and the engine implemented two. `evaluation_mode` was read twice in `rule_engine.evaluate` and never again, so STRUCTURED fell through the identical operator loop as DETERMINISTIC. The corpus contained zero STRUCTURED controls and there were zero tests — the third mode was a label.

Worse, `ck_deterministic_requires_rules` did not cover STRUCTURED, so such a control could be authored with nothing to check and would return INSUFFICIENT_EVIDENCE forever, reading as missing evidence rather than as the authoring error it was.

## Decision
STRUCTURED gets its own branch. It asks whether every fact the control declares is **present and well-formed**, using `control.facts` as the required-field list — no new column. All present → PASS; some → PARTIAL; none → INSUFFICIENT_EVIDENCE; present but unparseable → FAIL. A new `ck_structured_requires_facts` constraint, mirrored in the loader and the authoring schema, rejects an empty one.

## Reasoning
This is the honest distinction from DETERMINISTIC, which compares values. A password minimum of 4 is a perfectly good *structured* answer and a bad *deterministic* one; conflating the two would make the mode decorative. FAIL for a malformed value rather than INSUFFICIENT_EVIDENCE is deliberate: the evidence was provided and is structurally wrong, which the document itself demonstrates — that is a finding, not a gap.

## Alternatives Considered
- **A separate `structured_checks` column** — rejected; `facts` already declares exactly the fields in question, and a second overlapping list would drift.
- **Required-*document*-type checking** — deferred. `EvidenceDocument` carries `mime_type`, not a compliance classification, so there is nothing mechanical to check against yet.

## Consequences
One control (12.10.1) ships STRUCTURED so the path is exercised end to end rather than shipping as a dead capability.

## Reversal Cost
Low.

## Date
2026-09-01
