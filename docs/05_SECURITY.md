# 05_SECURITY.md

## 10.1 Threat Model

**Assets to protect:** client evidence documents (potentially containing IAM configs, network diagrams, access lists — i.e., a roadmap to the client's own security posture), the PCI corpus (low sensitivity, public standard), Finding/Report data (reflects a client's compliance gaps — reputationally sensitive if leaked), user credentials.

**Threat actors:** an unauthorized outsider (internet-facing app behind Cloudflare Tunnel); a firm employee accessing an engagement they aren't assigned to (insider-adjacent, low-sophistication but realistic — most GRC-tool incidents are access-control gaps, not exotic attacks); a compromised LLM/embedding API credential.

| Threat | Attack Surface | Impact | Mitigation | Priority |
|---|---|---|---|---|
| Cross-engagement data access | API authorization gaps | An auditor sees another client's security posture — severe trust/legal breach for the audit firm | Query-level ownership filtering (03_DATA_MODEL.md §8.2), tested explicitly (08_TESTING.md) | Critical |
| Credential stuffing / brute force | /api/auth/login | Account takeover → full access to that user's engagements | Lockout after 5 attempts/15min, Argon2id hashing, no user enumeration | High |
| Malicious file upload | Evidence document upload | Server compromise via crafted file (macro, embedded script, path traversal) | Content-type inspection (not extension-based), passive-parsing-only libraries, sanitized filenames, no execution of uploaded content | High |
| LLM prompt injection via evidence content | Finding-generation pipeline | A crafted "evidence" document could attempt to manipulate the LLM into producing a false positive finding | Treat all extracted document content as untrusted data in the LLM call, never as instructions; confidence threshold + human review is the actual backstop here — this is exactly why no Finding auto-approves | Medium (mitigated primarily by the human-review invariant, not by prompt-hardening alone) |
| Secret leakage (LLM/embedding API keys, DB credentials) | Environment/config, logs | Attacker gains API budget access or DB access | Secrets never logged, never in version control, loaded from environment only (§10.6) | High |
| Session hijacking | Cookie theft (XSS, network) | Account takeover | httpOnly, Secure, SameSite=Strict cookies; HTTPS enforced via Cloudflare Tunnel; standard XSS mitigations (§10.5) | Medium |

## 10.2 Authentication

- Email + password, Argon2id hashing (memory cost and iteration parameters set per current OWASP guidance at implementation time — not hardcoded here since parameters should be re-checked against current recommendations when this is actually built).
- Server-side sessions, httpOnly/Secure/SameSite=Strict cookie, 8-hour idle timeout, 24-hour absolute max.
- No self-service account recovery in POC — Admin resets manually via a documented internal procedure (see 00_PRODUCT.md §5.8 `DECISION REQUIRED`).
- Lockout: 5 failed attempts / 15 minutes per account.
- No custom cryptography anywhere — Argon2id and TLS (via Cloudflare) are the only cryptographic primitives in this system, both are standard libraries/infrastructure, not custom-built.

## 10.3 Authorization

- **Model:** simple role-based (auditor/reviewer/admin) + resource-level ownership via `EngagementAssignment`. No need for full ABAC at this scale — would be over-engineering per the "prefer simplicity" principle.
- **Ownership checks:** every Engagement-scoped query filters by assignment or reviewer/admin role at the query level (03_DATA_MODEL.md §8.2) — never fetched then filtered in application code after the fact.
- **Tenant isolation:** N/A at this stage (single-tenant); this section must be revisited before Stage 4 (multi-tenant).
- **Privilege escalation prevention:** role is set only by Admin action on the User record, never accepted as a client-supplied field on any other endpoint.
- **Explicit statement:** client-provided role, user ID, ownership, or permission information is never trusted as proof of authorization. Every authorization decision is re-derived server-side from the authenticated session, on every request.

## 10.4 Input Validation

- All request bodies validated against Pydantic schemas server-side (never rely on frontend validation alone).
- File uploads: content-type inspected (not trusted from filename/extension), size-limited (25MB), filename sanitized against path traversal.
- String length limits enforced on all free-text fields (`client_name` ≤ 200 chars, `tech_stack_summary` ≤ 5000 chars, etc. — exact limits set per field in the Pydantic schema, not left implicit).

## 10.5 Common Vulnerability Prevention

Relevant to this application's actual attack surface (not a generic checklist):

- **SQL injection:** SQLAlchemy parameterized queries throughout; no raw string-interpolated SQL anywhere in the codebase (enforced as an engineering rule, see 06_ENGINEERING_RULES.md).
- **XSS:** Next.js escapes rendered content by default; any place that renders LLM-generated or client-uploaded text (e.g., `ai_rationale`, extracted document snippets) must go through the same escaping path as any other user content — no `dangerouslySetInnerHTML` for AI/document-derived content.
- **CSRF:** SameSite=Strict cookies are the primary defense given this is a same-origin app; if the frontend and backend ever split across origins, add explicit CSRF tokens at that point.
- **IDOR/BOLA:** the single most relevant risk for this application, given every engagement contains another organization's sensitive security posture. Fully addressed via the query-level ownership filtering described in §10.3 and 03_DATA_MODEL.md §8.2 — this must be tested explicitly, not just implemented (08_TESTING.md).
- **Unsafe file uploads:** covered above (§10.4) — this is a real risk surface given the core feature is "upload documents from an unknown/external source."
- **SSRF:** relevant only if any feature ever fetches a URL supplied by user input (none currently do — if a future feature adds this, it needs its own review at that time).
- **Dependency vulnerabilities:** see §10.10.

Not addressed in depth (not relevant to this application's actual surface): open redirects (no redirect-URL parameters anywhere in this API), command injection (no shell-out to user-influenced commands anywhere in the pipeline).

## 10.6 Secrets

- Stored as environment variables, loaded via the `/config` module, never hardcoded, never committed to version control (`.env` in `.gitignore` from day one).
- Local development: a `.env.example` with placeholder values, real `.env` never committed.
- Production: injected via the deployment environment (Docker Compose environment file, not baked into the image).
- Rotation: LLM/embedding API keys and the session-signing secret should be rotatable without a code change (read from environment at process start).
- Prohibited: no secrets in logs, no secrets in error messages, no secrets in the frontend bundle.

## 10.7 Logging

Must never be logged: `password_hash`, session tokens/cookies, LLM/embedding API keys, full request/response bodies containing `extracted_text` or `tech_stack_summary`, full LLM prompts or completions (log only metadata: duration, status, token count).

## 10.8 Rate Limiting and Abuse Prevention

- Login: 5 attempts / 15 min per account (§10.2).
- Scope-suggestion endpoint: capped per-user to prevent runaway LLM cost (04_API_CONTRACT.md).
- No public-facing rate limiting needed beyond this — the app has no public/anonymous endpoints.

## 10.9 Security Headers and Transport

- HTTPS enforced end-to-end via Cloudflare Tunnel (no direct unencrypted access to the origin server).
- Cookies: httpOnly, Secure, SameSite=Strict.
- Standard security headers (X-Content-Type-Options, X-Frame-Options or CSP frame-ancestors, Content-Security-Policy scoped to the app's actual script/style sources) set at the FastAPI/Next.js response layer.
- CORS: restricted to the app's own frontend origin only — no wildcard.

## 10.10 Dependency Security

- Lockfiles committed (`requirements.txt`/`poetry.lock` or `uv.lock` for backend, `package-lock.json` for frontend) — consistent with the existing uv-based workflow already used on the self-hosted infra.
- New dependencies added only when the existing project dependencies genuinely can't solve the problem (see 06_ENGINEERING_RULES.md).
- Periodic vulnerability scanning (e.g., `pip-audit`, `npm audit`) — run at minimum before each deployment, not just ad hoc.

## 10.11 Security Release Checklist

```text
[ ] All new endpoints have an explicit authentication requirement documented and enforced
[ ] All new endpoints have an explicit authorization rule documented and enforced at the query level
[ ] No new endpoint trusts a client-supplied role/ownership/user-ID claim
[ ] No secrets introduced in code, logs, or error messages
[ ] File-handling changes re-checked against §10.4/§10.5
[ ] Any new external service call has a documented timeout, retry, and failure-fallback behavior
[ ] Dependency scan run and clean (or documented exceptions)
[ ] The human-sign-off invariant (no Finding approved without reviewed_by; no Engagement finalized except by a Reviewer) re-verified if the Finding or Engagement finalization code path was touched
```
