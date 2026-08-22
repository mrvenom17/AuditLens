# 02_ARCHITECTURE.md

## 7.1 Architecture Overview

A single-tenant monolith. This is deliberate: one firm, one deployment, low concurrent load (~5–20 users), and a POC goal of "prove the workflow," not "prove it scales." Do not split into services.

```text
Browser (Auditor / Reviewer / Admin)
   ↓ HTTPS via Cloudflare Tunnel
Next.js Frontend (App Router, TypeScript)
   ↓ REST calls
FastAPI Backend
   ↓
Authentication + Session Middleware
   ↓
Route/API Layer
   ↓
Service Layer (business logic: scoping, matching, finalization rules)
   ↓
Repository / Data Access Layer (SQLAlchemy)
   ↓
PostgreSQL (+ pgvector extension)
   ↓
Local filesystem volume (original evidence files, content-hash addressed)

Background workers (same host, separate process):
   Extraction/OCR pipeline → Embedding pipeline → LLM matching calls
   ↓
   Writes results back through the Service Layer (not directly to DB)
```

## 7.2 Technology Stack

| Technology | Purpose | Why selected | Alternatives rejected | Security implications | Operational implications |
|---|---|---|---|---|---|
| FastAPI (Python 3.12) | Backend API | Matches existing Anagha stack — reuses your existing skill and debugging experience | Django (heavier than needed for a POC), Express/Node (would mean building Python OCR/ML tooling in a second language) | Async-native, good for I/O-bound LLM calls | Same deployment pattern as Anagha — you already have working ops muscle memory here |
| Next.js (App Router, TS) | Frontend | Same reasoning — stack reuse | Plain React SPA (loses SSR/routing conveniences with no offsetting benefit here) | N/A | Same |
| PostgreSQL 16 + pgvector | Primary datastore + vector search | One database for both relational data and embeddings avoids running a separate vector DB for this scale | Dedicated vector DB (Pinecone/Weaviate) — unjustified operational overhead at this scale | Single system to secure/back up | Alembic migrations already familiar from Anagha |
| SQLAlchemy 2.0 + Alembic | ORM + migrations | Consistency with existing stack | Raw SQL (loses migration tracking) | Parameterized queries by default (SQL-injection resistant) | Same migration workflow as Anagha |
| Argon2id | Password hashing | Current best-practice password hash, memory-hard | bcrypt (acceptable but Argon2id is the stronger current default) | Resistant to GPU-based cracking | None significant |
| Self-hosted embedding model (e.g., a small BGE/sentence-transformers model) | Convert text to vectors for retrieval | Predictable cost at low volume — no per-call billing surprise | Hosted embedding API — viable but adds a recurring cost line and an external dependency for a core pipeline step; revisit at Stage 2+ if self-hosted quality is insufficient | Runs entirely on your own infra — no evidence content leaves your server for this step | Requires a bit more setup than an API call; one-time cost |
| LLM API (e.g., Claude) | Scope suggestion, evidence-request drafting, finding generation | Reasoning-heavy tasks where a hosted frontier model outperforms a self-hosted one at this stage | Fully self-hosted LLM — feasible later, not worth the complexity for a POC | Evidence content IS sent to this external API — must be disclosed to the client firm and covered in your own data-handling agreement with them | Recurring per-call cost; must have a hard fallback path for every feature that uses it (see §7.6, §7.7) |
| Docker Compose | Local packaging for deployment | Matches typical self-hosted Ubuntu deployment pattern; simple, no orchestration overhead | Kubernetes — explicitly unjustified at this scale | Isolates services | Reuses your existing Cloudflare Tunnel + Ubuntu operational pattern |

## 7.3 Repository Structure

```text
/backend
  /app
    /api            # route handlers only — thin, no business logic
    /services        # business logic: scoping, matching, finalization rules
    /repositories     # data access, one per entity
    /models           # SQLAlchemy ORM models
    /schemas          # Pydantic request/response models
    /pipelines        # extraction, embedding, LLM-matching background jobs
    /corpus           # PCI DSS v4.0.1 clause data + versioning
    /auth             # session/auth logic
    /config           # settings, environment loading
  /migrations         # Alembic
  /tests
/frontend
  /app                # Next.js App Router pages
  /components
  /lib                # API client, auth helpers
  /types              # shared TS types (mirrors backend Pydantic schemas)
/docs                 # this documentation set
/deploy               # Docker Compose, Cloudflare Tunnel config
```

## 7.4 Layer Responsibilities

### Route/API Layer
**MUST:** parse and validate the request (via Pydantic schemas), authenticate, authorize (call into the auth/authorization service — never inline role checks scattered across routes), call the appropriate service, return a sanitized response.
**MUST NOT:** contain business logic (scoping rules, matching thresholds, finalization rules), perform direct database queries, trust any client-supplied ownership/role claim, expose raw internal exceptions to the client.

### Service Layer
**MUST:** implement all business rules from 01_REQUIREMENTS.md (e.g., "no Finding reaches approved without reviewed_by set"), orchestrate calls to repositories and to the pipelines module, enforce the human-sign-off invariant at this layer (not just in the API layer or the UI) so it cannot be bypassed by a future route that forgets to check.
**MUST NOT:** know about HTTP (no request/response objects here — keeps it testable and reusable), directly construct SQL.

### Repository/Data Layer
**MUST:** be the only layer that touches the ORM/session directly, enforce ownership filters at the query level (e.g., an Auditor's engagement query is filtered by assignment in the query itself, not filtered after the fact in Python).
**MUST NOT:** contain business logic beyond straightforward data-shape concerns.

### Frontend
**MUST:** treat every server response as the source of truth for what the user is allowed to do (hide/disable UI based on role, but never rely on that as the actual security boundary), handle the "needs_manual_review" and "extraction_failed" states as first-class UI states, not edge cases bolted on later.
**MUST NOT:** implement any authorization logic that isn't re-verified server-side, cache and reuse another user's session data.

## 7.5 Data Flow

**Authentication:** browser → Next.js → FastAPI `/auth/login` → session created → httpOnly cookie set → subsequent requests carry the cookie → middleware resolves it to a user+role before any route handler runs.

**Core business operation (evidence → finding):** upload → repository stores file + metadata row → background worker picks up `extraction_status=processing` rows → extraction → embedding → retrieval against scoped requirements → LLM call → Finding row(s) written via the service layer (not directly by the worker) so business rules are enforced in one place.

**Failure recovery:** any pipeline step failure sets an explicit status (`extraction_failed`, `needs_manual_review`) rather than retrying silently forever or leaving a row in `processing` indefinitely — a background sweep job flags anything stuck in `processing` past a timeout (e.g., 10 minutes) as `failed` for manual attention.

## 7.6 External Services

### LLM API (scope suggestion, request drafting, finding generation)
- **Purpose:** the three reasoning-heavy steps described in 01_REQUIREMENTS.md.
- **Integration boundary:** called only from the `/pipelines` and relevant `/services` modules — never directly from route handlers.
- **Authentication:** API key stored as an environment secret (see 05_SECURITY.md §10.6), never logged.
- **Timeout strategy:** 8-second timeout on scope suggestion (interactive path); 30-second timeout on finding generation (background path).
- **Retry strategy:** one retry with backoff for transient errors (5xx, timeout); no retry on 4xx (bad request — retrying won't fix it).
- **Failure behavior:** every feature that calls the LLM has a defined non-LLM fallback state (see each feature's Failure Cases in 01_REQUIREMENTS.md) — the product must remain usable, just less automated, if this service is down.
- **Rate limiting:** background matching jobs are queued and throttled to stay under the provider's rate limit; interactive calls (scope suggestion) are not queued but are capped per-user to prevent accidental abuse.
- **Secret handling:** loaded from environment/secrets file, never committed, never returned in any API response or log line.

### Embedding model
- **Purpose:** vectorize extracted evidence text and corpus clauses for retrieval.
- **Integration boundary:** self-hosted, called from the `/pipelines` module.
- **Failure behavior:** if the embedding service is down, extraction still completes and is stored, but matching is deferred (retried on a schedule) rather than failing the whole upload.

## 7.7 Error Handling Architecture

- **User-visible errors:** validation errors (400), authorization errors (403), not-found (404), conflict (409) — all with a stable error `code` and a human-readable `message`.
- **Logged-only errors:** any 5xx, any unexpected exception — logged with a `request_id` and full stack trace server-side, but the client only ever sees a generic "something went wrong, reference ID: {request_id}" message.
- **Response format (standardized):**
```json
{
  "error": {
    "code": "REQUIREMENT_NOT_SCOPED",
    "message": "This engagement has no confirmed scope yet.",
    "request_id": "a1b2c3d4"
  }
}
```
- **Retryable vs non-retryable:** 5xx and timeouts are retryable by the client; 4xx are not (the request itself needs to change).

## 7.8 Logging and Observability

- **Log:** every authentication attempt (success/failure, not password), every authorization denial, every Finding status transition with actor and timestamp, every external API call's latency and status (not its payload).
- **Never log:** passwords, session tokens, full LLM prompts/responses containing client evidence content (log metadata about the call — duration, status, token count — not the content), raw file contents.
- **Correlation:** every request gets a `request_id` propagated through logs and returned in error responses.
- **Health checks:** `/health` (liveness — process is up) and `/health/ready` (readiness — DB and background worker queue reachable).
- **Key metrics:** engagement count by status, Finding review queue depth, LLM call failure rate, extraction failure rate.

## 7.9 Performance and Scaling

At this scale (one firm, a handful of concurrent engagements), performance risk is low. The one real bottleneck is the LLM/extraction pipeline for large evidence batches — this is why it's a background queue, not inline with the upload request. Database indexing: index `Engagement.status`, `Finding.status`, `Finding.engagement_id`, `EvidenceDocument.engagement_id` — these are the actual query patterns the UI will hit. No caching layer needed at this scale. No horizontal scaling planned for this stage — a single application server is sufficient and matches the existing self-hosted infra pattern.
