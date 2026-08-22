# 09_DEPLOYMENT.md

## Environments

- **Local:** developer machine, Docker Compose, local Postgres.
- **Production:** existing self-hosted Ubuntu server, reached via Cloudflare Tunnel (same pattern already used for Anagha Safar and Pharmacare) — `DECISION REQUIRED`: whether this shares one of the two existing Ubuntu servers or gets its own, given it will hold a different audit firm's sensitive client data (recommend a separate server or at minimum a separate isolated container/VM, not co-located with unrelated apps, given the sensitivity classification in 03_DATA_MODEL.md §8.4).
- No separate staging environment for the POC stage — not justified at this scale; introduce one at Stage 2+ when a second real firm/client is involved and a mistake in production carries more weight.

## Environment Variables

| Variable | Purpose | Required In | Secret? | Example Format |
|---|---|---|---|---|
| `DATABASE_URL` | Postgres connection string | All | Yes | `postgresql://user:pass@host:5432/db` |
| `SESSION_SECRET` | Session-signing key | All | Yes | 32+ byte random string |
| `LLM_API_KEY` | LLM provider credential | All | Yes | provider-specific token format |
| `EMBEDDING_MODEL_PATH` | Path/identifier for the self-hosted embedding model | All | No | e.g. `/models/bge-small` |
| `FILE_STORAGE_PATH` | Root path for evidence file storage | All | No | `/data/evidence` |
| `MAX_UPLOAD_SIZE_MB` | Upload size limit | All | No | `25` |
| `CORS_ALLOWED_ORIGIN` | Frontend origin allowed for CORS | All | No | `https://auditlens.yourdomain.tld` |
| `ENVIRONMENT` | local / production | All | No | `production` |

Never use real secrets in this table or in `.env.example` — placeholders only.

## Build Process

1. Install backend dependencies (`uv sync` or equivalent, matching the existing project's package-manager convention).
2. Install frontend dependencies (`npm ci`).
3. Run Alembic migrations against the target database.
4. Build the Next.js production bundle (`next build`).
5. Start both services under Docker Compose.

## Database Deployment

- Migrations run as an explicit step before app startup, never automatically inferred/applied at runtime.
- Backups: a scheduled `pg_dump` to a separate location, given this database holds professional audit records — this is not optional at this sensitivity level even at POC scale.
- Rollback: keep the previous migration's down-revision documented; a failed migration should halt startup rather than leave the app running against a half-migrated schema (this matches a documented past failure pattern — the Alembic partial-application-loop issue already seen on the existing self-hosted infra — so this deployment process should explicitly guard against repeating it).

## Health Checks

- **Liveness:** `/health` — process responsive.
- **Readiness:** `/health/ready` — database reachable, background worker queue reachable.
- **Dependency checks:** a lightweight check that the LLM/embedding services are reachable, surfaced on an admin status page rather than blocking readiness (the app must stay up even if these degrade — see 02_ARCHITECTURE.md §7.6).

## Logging and Monitoring

- Application logs to stdout/stderr, captured by Docker, rotated (avoid unbounded log growth on the existing server).
- Error monitoring: at minimum, a log-based alert on any 5xx spike or repeated extraction/LLM failures — a dedicated error-tracking service is a reasonable Stage 2 upgrade, not required for POC.
- Alert conditions worth having even at POC scale: disk space on the evidence-storage volume (evidence files accumulate and this is a small server), and any spike in 403s (could indicate an authorization bug or an attempted access-boundary probe).

## Deployment Security

- HTTPS enforced via Cloudflare Tunnel — no direct unencrypted origin exposure.
- Secrets injected via the Compose environment file (not baked into the image, not in the repo).
- `ENVIRONMENT=production` disables any debug/verbose-error mode — stack traces never returned to the client in production (05_SECURITY.md §10.7's logging rules apply here too — verbose local debug logging must not accidentally ship to production).
- Least privilege: the database user the app connects as should not be a Postgres superuser.

## Rollback Strategy

If a deployment fails health checks post-deploy, the previous container image/version is redeployed and the failed migration (if any) is investigated before retrying — never left running in a partially-migrated state.

## Production Readiness Checklist

```text
[ ] All environment variables set (table above) with real production values
[ ] Migrations applied successfully, verified via readiness check
[ ] Backup schedule configured and tested with one real restore
[ ] HTTPS/Cloudflare Tunnel confirmed working end to end
[ ] Debug mode confirmed off
[ ] Database user is not superuser
[ ] Log rotation configured
[ ] Disk space alert configured for the evidence-storage volume
[ ] Security release checklist (05_SECURITY.md §10.11) passed for this release
```

## Definition of Successfully Deployed

The application is reachable via its production URL over HTTPS, `/health` and `/health/ready` both return 200, a test login succeeds, and one real (or realistic test) engagement can be created and scoped end to end without manual database intervention.
