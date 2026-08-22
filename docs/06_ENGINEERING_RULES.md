# 06_ENGINEERING_RULES.md

This document is the AI coding agent's constitution for this project. When any rule here conflicts with a convenient shortcut, this document wins.

## Architecture Discipline
- The coding agent MUST inspect existing patterns (repository structure, naming, existing service/repository conventions) before creating a new pattern.
- The coding agent MUST NOT put business logic in the Route/API layer or the Repository layer — it belongs in the Service layer only (see 02_ARCHITECTURE.md §7.4).
- The coding agent MUST NOT modify unrelated files without a documented reason in the commit/PR description.

## Code Quality
- The coding agent MUST follow existing formatting/linting configuration rather than introducing a new style.
- The coding agent MUST NOT leave dead code, commented-out blocks, or TODO-without-ticket markers in a completed task.

## Dependency Rules
- The coding agent MUST NOT introduce a new dependency unless the existing project dependencies cannot reasonably solve the problem.
- Any new dependency MUST be added to the lockfile and briefly justified in the relevant task's completion notes.

## Type Safety
- Backend: every route input/output MUST have a Pydantic schema — no raw dicts passed across layer boundaries.
- Frontend: TypeScript types MUST mirror backend schemas (kept in `/frontend/types`) — no `any` on data crossing the API boundary.

## Validation
- The coding agent MUST validate external input at the server boundary regardless of what the frontend already validates.

## Authentication
- The coding agent MUST NOT bypass authentication for development convenience (no "temporarily disable auth" commits, even in a branch — use seeded test accounts instead).

## Authorization
- The coding agent MUST NOT trust client-provided identity, role, ownership, or authorization information for any decision.
- The coding agent MUST implement every new Engagement-scoped query with the ownership filter applied at the query level (03_DATA_MODEL.md §8.2) — never "fetch all, then filter in Python."
- The coding agent MUST treat the Finding-approval and Engagement-finalization invariants (no approval without `reviewed_by`; finalize is Reviewer-only) as non-negotiable — any code path that could bypass them is a bug regardless of how it was introduced.

## Database Access
- The coding agent MUST NOT write raw string-interpolated SQL.
- The coding agent MUST write additive-first migrations (see 03_DATA_MODEL.md §8.5) — no destructive schema changes without a separate, explicitly reviewed migration.

## Error Handling
- The coding agent MUST NOT silently swallow errors (no bare `except: pass`).
- The coding agent MUST distinguish user-facing errors (400/403/404/409, per 02_ARCHITECTURE.md §7.7) from internal errors (logged with `request_id`, generic message to the client).
- Every external service call (LLM, embedding, extraction) MUST have an explicit, tested failure path — per-feature fallback behavior is specified in 01_REQUIREMENTS.md and must not be skipped "for now."

## Logging
- The coding agent MUST NOT log any field listed as Secret or Sensitive in 03_DATA_MODEL.md §8.4, or any value listed in 05_SECURITY.md §10.7.

## Secrets
- The coding agent MUST NOT hardcode any credential, API key, or connection string.
- The coding agent MUST add any new required environment variable to `.env.example` with a placeholder value and to 09_DEPLOYMENT.md's environment variable table.

## Testing
- The coding agent MUST add or update tests for any change to authorization logic, the Finding review state machine, or the finalization flow — these are the highest-risk paths in the system.
- The coding agent MUST NOT mark a task complete with a failing or skipped test on these paths.

## Refactoring
- The coding agent MUST fix root causes rather than adding a temporary patch when the root cause is reasonably identifiable within the current task's scope.
- Refactors outside the current task's scope MUST be proposed as a separate task, not folded silently into an unrelated change.

## Scope Control
- The coding agent MUST implement exactly one task (07_TASKS.md) at a time and MUST NOT begin a subsequent task's work "while already in the area."
- The coding agent MUST NOT implement Stage 2+ features (multi-framework, multi-tenant, live client connectors, in-app sending, auto-finalization) — these are explicitly out of scope for this documentation set (00_PRODUCT.md §5.5).

## Git/Change Discipline
- Commits should correspond to one task or one clearly-described fix.
- The coding agent MUST update the relevant documentation file(s) when an actual architectural or API decision changes during implementation (e.g., a different error code was needed) — silent drift between docs and code is exactly what this documentation set exists to prevent.

## Completion Rules — Definition of Done

```text
[ ] Requirements (01_REQUIREMENTS.md) for this feature checked
[ ] Architecture (02_ARCHITECTURE.md) layer boundaries followed
[ ] Security rules (05_SECURITY.md) followed, including the release checklist if touching auth/authz/uploads
[ ] Input validation implemented server-side
[ ] Authentication enforced where required
[ ] Authorization enforced at the query level where required
[ ] Tests added/updated, especially for authz, Finding review, and finalization paths
[ ] Existing tests pass
[ ] Lint passes
[ ] Type checks pass (mypy/pyright for backend, tsc for frontend)
[ ] No secrets added
[ ] Documentation updated where a real decision changed
[ ] No unrelated files changed
```
