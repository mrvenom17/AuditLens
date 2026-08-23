# AuditLens deployment

Operational runbook for 09_DEPLOYMENT.md. Every command here was run against
the containerised stack during TASK-024; nothing in this file is aspirational.

## Layout

| File | Purpose |
|---|---|
| `docker-compose.yml` | Base stack. Publishes loopback ports for local development. |
| `docker-compose.prod.yml` | Production overlay. Removes all host ports, requires every secret, adds log rotation, resource limits, and read-only roots. |
| `.env.example` | Template for the production environment file. Copy to `.env`. |
| `cloudflared/config.example.yml` | Tunnel ingress rules, for the credentials-file setup. Not needed when using `TUNNEL_TOKEN`. |
| `scripts/backup.sh` | Scheduled `pg_dump`, run as a long-lived container. |

## Local development

```sh
cp ../backend/.env.example ../backend/.env    # then fill in
docker compose up -d db
docker compose run --rm migrate
docker compose up -d api worker web
docker compose exec worker python -m app.corpus.loader --embed
docker compose exec api python -m app.scripts.seed_admin \
    create --email you@firm.example --name "Your Name" --role admin
```

The API is on `127.0.0.1:8000`, the frontend on `127.0.0.1:3000`, Postgres on
`127.0.0.1:5433` (5433, not 5432, so it never collides with a locally-installed
Postgres).

## Production

```sh
cp .env.example .env        # then fill in every CHANGE_ME
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Migrations run as a **separate, explicit step before the app starts**, never
automatically at runtime (09_DEPLOYMENT.md § Database Deployment). If that
command fails, stop: the deploy has halted at the right point, which is the
whole reason it is a separate step. Do not start the app against a
half-migrated schema.

Then, once only, on a new deployment:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    exec worker python -m app.corpus.loader --embed
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
    exec api python -m app.scripts.seed_admin create \
    --email admin@firm.example --name "Firm Admin" --role admin
```

The first corpus load downloads the embedding model (~130MB) into the
`model_cache` volume. Subsequent restarts reuse it.

### What the production overlay changes

* **No host ports at all.** `api`, `web` and `db` all use `ports: !reset null`.
  A bare `ports: []` would not work — Compose *merges* sequences across files,
  so an empty list is a no-op and the base file's loopback mappings would
  survive. Verify with:

  ```sh
  docker compose -f docker-compose.yml -f docker-compose.prod.yml config \
    | grep -A2 'ports:'          # expect no output
  ```

* **Every secret is required.** Each is declared `${VAR:?...}`, so a missing
  one aborts `up` with a message naming the variable, rather than starting a
  container that is quietly misconfigured. The application additionally
  re-checks at startup (`validate_for_environment`) and refuses to serve on a
  development `SESSION_SECRET` or `DATABASE_URL`, or an `http://` CORS origin.

* **Log rotation** — 10MB × 5 files per service. 09_DEPLOYMENT.md calls out
  unbounded log growth on a small server as a real risk.

* **Read-only root filesystems** on `api` and `web`, with an explicit tmpfs.
  These containers parse untrusted uploaded files; a read-only root is the
  cheapest limit on what a parser bug can do next. The `worker` is writable
  because the embedding model downloads into `/models`.

## Ingress

The only path in is the Cloudflare Tunnel, which connects **outbound**. The
origin never listens on a public interface, so 09_DEPLOYMENT.md's "no direct
unencrypted origin exposure" is a property of the topology rather than of a
firewall rule someone must remember to maintain.

Two ways to configure it:

1. **Token (default).** Create the tunnel in the Cloudflare Zero Trust
   dashboard, set its public hostname to route `/api/*` → `api:8000` and
   everything else → `web:3000`, and put the token in `TUNNEL_TOKEN`.
2. **Credentials file.** Copy `cloudflared/config.example.yml` to
   `cloudflared/config.yml`, fill in the tunnel id and hostname, mount the
   directory into the `tunnel` service, and drop the `TUNNEL_TOKEN` env var.

Order matters in the ingress rules: `/api/*` must come before the catch-all
frontend route, or the API is swallowed by it.

## Backups

The `backup` service runs `pg_dump` on a loop (daily by default) into
`./backups`, keeping `BACKUP_RETENTION_DAYS` of history. It dumps to a
`.partial` name and renames on success — a truncated backup that looks complete
is worse than no backup, because it will be trusted.

**Getting backups off this host is a separate step you must configure.** A dump
sitting on the same disk as the database it came from protects against operator
error, not against losing the disk.

Restore:

```sh
gunzip -c backups/auditlens-YYYYMMDDTHHMMSSZ.sql.gz \
  | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

09_DEPLOYMENT.md requires the backup schedule be **tested with one real
restore** before the deployment is considered ready. Do that against a scratch
database, not production.

## Health and monitoring

* `GET /health` — liveness. Process responsive, touches nothing external.
* `GET /health/ready` — readiness. Database reachable and the worker queue
  table queryable. Returns 503 when either fails.

Readiness deliberately does **not** check the LLM or embedding services:
09_DEPLOYMENT.md requires those to be surfaced separately rather than block
readiness, because the app must stay up when they degrade. A degraded LLM makes
the product less automated, not unavailable — scope suggestion returns
`manual_scoping_required`, evidence requests fall back to templates, and
findings are created with a null suggestion and the manual-review flag set.

Alert conditions worth having from day one (09_DEPLOYMENT.md):

* Disk space on the evidence volume — evidence accumulates and is never deleted.
* Any spike in 403s — could be an authorization bug or a boundary probe. The
  application logs every authorization denial as `authz.denied`.
* Repeated `extraction.failed` or `external.call ... status=failed` lines.

## Rollback

If the deploy fails its health checks, redeploy the previous image tag. If a
migration failed, investigate before retrying — never leave the app running
against a partially-migrated schema.

## Production readiness checklist

From 09_DEPLOYMENT.md, with how to verify each:

```text
[ ] Every environment variable set        -> `up` aborts naming any that is missing
[ ] Migrations applied                    -> /health/ready returns 200
[ ] Backup schedule configured and tested with one real restore
[ ] HTTPS via Cloudflare Tunnel confirmed end to end
[ ] Debug mode off                        -> ENVIRONMENT=production; /docs returns 404
[ ] Database user is not a superuser      -> \du in psql
[ ] Log rotation configured               -> set in the production overlay
[ ] Disk space alert on the evidence volume
[ ] Security release checklist (05_SECURITY.md §10.11) passed
```

The last four columns of that list are host-level and operator-owned; the
application cannot verify them for you.
