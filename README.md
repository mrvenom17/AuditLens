# AuditLens

A PCI DSS v4.0.1 audit assistant built on one rule: **the machine determines,
the human decides, and the AI only explains.**

Compliance results come from a deterministic rule engine running human-authored
rules over evidence with full provenance. No language model can produce, alter,
or approve a result — that is enforced structurally, not by policy, and proven
by tests that run on every build.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [OCR providers](#ocr-providers)
- [Everyday commands](#everyday-commands)
- [Testing](#testing)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)

---

## How it works

```
Company profile → applicability → scope → evidence request → upload
      → extract & OCR → facts (with provenance) → rule engine
      → evidence gate → strength → auditor review → frozen report
```

Three kinds of claim, never mixed:

| | Authority | Where |
|---|---|---|
| **Rule engine** | Binding. Produces the compliance result. | `app/services/rule_engine.py` |
| **Auditor** | Final. Recorded separately, never overwrites the machine. | `app/services/finding.py` |
| **GenAI** | Advisory only. Drafts text, proposes, explains. | 3 call sites, none can write a result |

`ControlEvaluation.result` has **no API write path at all** — not a
permission-gated one, none. No request schema in the API contains the field, and
a test walks the live OpenAPI schema on every run to keep it that way.

The full walkthrough lives in [`docs/`](docs/), starting with
[`00_PRODUCT.md`](docs/00_PRODUCT.md).

---

## Requirements

| | Version | Notes |
|---|---|---|
| Python | 3.12+ | |
| Node | 20+ | Frontend only |
| PostgreSQL | 16 + `pgvector` | Supplied by the compose stack |
| Docker | Any recent | For the database, or the whole stack |
| Tesseract | 5.x | Only if you use local OCR — see [OCR providers](#ocr-providers) |

Tesseract is optional. If you set `OCR_PROVIDER` to an API provider and turn the
local fallback off, you never need the binary.

```bash
# macOS
brew install tesseract
# Debian / Ubuntu
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng
```

---

## Quick start

### 1. Database

```bash
cd deploy
docker compose up -d db
```

Postgres listens on **127.0.0.1:5433** (not 5432, so it never collides with a
local install).

### 2. Backend

```bash
cd backend
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

cp .env.example .env
```

Now edit `.env`. Two values must change before anything works:

```bash
# 32+ bytes of randomness
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

- `SESSION_SECRET` — paste the generated value
- `DATABASE_URL` — set the password to match `POSTGRES_PASSWORD` (default
  `auditlens_local_dev` for local development)

`LLM_API_KEY` is **optional**. Leave it blank and the system runs fully — see
[Running without an API key](#running-without-an-api-key).

### 3. Migrate, load the corpus, create a user

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m app.corpus.loader           # 205 controls
.venv/bin/python -m app.scripts.seed_admin create \
    --email you@firm.com --name "Your Name" --role admin
```

Add `--embed` to the corpus loader to compute embeddings now. It downloads a
~130 MB model on first run; skip it unless you want evidence discovery, which is
navigational only and never affects a result.

### 4. Run

Three processes, three terminals:

```bash
# API                                    → http://localhost:8000
cd backend && .venv/bin/uvicorn app.main:app --reload

# Worker (extraction, OCR, facts, evaluation)
cd backend && .venv/bin/python -m app.pipelines.worker

# Frontend                               → http://localhost:3000
cd frontend && npm install && npm run dev
```

Open http://localhost:3000 and sign in with the account you just created. There
is no self-registration anywhere in the system — accounts come from the CLI or
from an Admin.

---

## Configuration

Every setting lives in `backend/.env`. `.env` is gitignored;
`backend/.env.example` is the committed template and is the authoritative list.
`deploy/.env.example` carries the same variables for the container stack.

### Required

| Variable | Notes |
|---|---|
| `DATABASE_URL` | The app's database user must not be a superuser. |
| `SESSION_SECRET` | 32+ random bytes. Production refuses to start with the dev default. |

### External services

| Variable | Default | Notes |
|---|---|---|
| `LLM_API_KEY` | *(blank)* | Anthropic key. Optional — see below. |
| `LLM_MODEL` | `claude-sonnet-5` | |
| `EMBEDDING_MODEL_PATH` | `BAAI/bge-small-en-v1.5` | Self-hosted, worker only. |
| `EMBEDDING_DIMENSIONS` | `384` | Must match the model. |

### OCR

| Variable | Default | Notes |
|---|---|---|
| `OCR_PROVIDER` | `tesseract` | `tesseract`, `google_vision`, or `ocr_space`. |
| `OCR_API_KEY` | *(blank)* | Required for the API providers. |
| `OCR_API_URL` | *(blank)* | Override to use a proxy or self-hosted endpoint. |
| `OCR_TIMEOUT_SECONDS` | `30` | |
| `OCR_FALLBACK_TO_LOCAL` | `true` | Retry locally when the API fails. |

### Storage and versions

| Variable | Default | Notes |
|---|---|---|
| `FILE_STORAGE_PATH` | `./data/evidence` | Must be a persistent volume. Evidence is never deleted. |
| `MAX_UPLOAD_SIZE_MB` | `25` | |
| `RULE_ENGINE_VERSION` | `1.0.0` | Stamped on every evaluation and report. |
| `CONTROL_CORPUS_VERSION` | `pci-dss-v4.0.1-poc-2` | Must match the corpus file. |
| `CORS_ALLOWED_ORIGIN` | `http://localhost:3000` | No wildcard. Must be https in production. |

### Running without an API key

The deterministic core never calls a model, so with `LLM_API_KEY` blank
everything essential still works. What degrades:

| Feature | Without a key |
|---|---|
| Rule evaluation, evidence gate, applicability | **Unaffected** — never used a model |
| Scope suggestion | Deterministic applicability still runs; the advisory pass is skipped |
| Evidence requests | Template wording instead of AI-drafted |
| Result explanations | Omitted; the result and its evidence are unchanged |

This is a supported mode, not a broken one. The adversarial test suite runs with
every model call wired to raise, precisely to keep it that way.

---

## OCR providers

Image evidence — screenshots, scans, phone photos of a console — needs OCR
before any fact can be extracted from it.

### `tesseract` (default)

Local binary. No key, no network, nothing leaves your server. Good on clean
screenshots, weak on skewed scans, phone photos and low-contrast terminals.

```bash
OCR_PROVIDER=tesseract
```

### `google_vision`

Best accuracy on real-world evidence. Uses the dense-text model
(`DOCUMENT_TEXT_DETECTION`), which reads a configuration screenshot far better
than the general text model.

1. Enable the Cloud Vision API in your Google Cloud project.
2. Create an API key and restrict it to the Vision API.

```bash
OCR_PROVIDER=google_vision
OCR_API_KEY=AIza...
```

### `ocr_space`

Simplest to start with, and has a free tier — a reasonable first step before
committing to a cloud account. Get a key at
[ocr.space/ocrapi](https://ocr.space/ocrapi).

```bash
OCR_PROVIDER=ocr_space
OCR_API_KEY=K8...
```

### Choosing

**Sending client evidence to a third party is a disclosure decision.** The
default is local so that choice is made deliberately rather than inherited. If
you switch to an API provider, make sure your engagement terms cover it.

When an API provider fails, `OCR_FALLBACK_TO_LOCAL=true` retries with Tesseract
and logs the substitution — a degraded read beats an upload that silently yields
nothing. Set it `false` when evidence must never be read by a weaker engine
without someone noticing; the document then fails with a configuration error the
operator can act on.

Whichever provider runs, its output is treated as **evidence content, never
instructions**. It goes through the same fact scanner as every other document
and cannot reach a compliance result.

---

## Everyday commands

```bash
# Backend, from backend/
.venv/bin/uvicorn app.main:app --reload        # API with hot reload
.venv/bin/python -m app.pipelines.worker       # Worker
.venv/bin/alembic upgrade head                 # Apply migrations
.venv/bin/alembic revision --autogenerate -m "what changed"

.venv/bin/python -m app.corpus.loader          # Load / refresh the corpus
.venv/bin/python -m app.corpus.loader --embed  # ...and compute embeddings

# Users — there is no self-registration
.venv/bin/python -m app.scripts.seed_admin create --email a@firm.com --name "Ada" --role admin
.venv/bin/python -m app.scripts.seed_admin reset-password --email a@firm.com
.venv/bin/python -m app.scripts.seed_admin list

# Frontend, from frontend/
npm run dev
npm run build
```

Roles are `auditor`, `reviewer`, `admin`. Only a **reviewer** can finalize an
audit — deliberately, an admin cannot.

---

## Testing

```bash
cd backend
.venv/bin/python -m pytest -q                  # 682 tests
.venv/bin/ruff check app tests migrations
.venv/bin/ruff format --check app tests migrations
.venv/bin/mypy app

cd ../frontend
npx tsc --noEmit && npx eslint . && npm run build
```

Tests need the database running (`docker compose up -d db` in `deploy/`). They
create and drop their own schema.

### The tests that matter most

`tests/adversarial/` runs with the LLM and embedding clients wired to **raise on
every call** — absence of a model is the baseline, not a special case. It covers
the five AI-safety scenarios and all eleven rows of the Level 0 acceptance table:

- A prompt-injection payload in a document produces a result identical to the
  same document without it.
- Evidence that discusses a setting without stating a value yields
  `INSUFFICIENT_EVIDENCE`, never a guess.
- A citation to page 17 of a one-page document is caught by the gate.
- Two documents disagreeing produce `CONFLICT` regardless of processing order.
- Rewriting a stored file after extraction is caught by hash mismatch.

---

## Deployment

See [`deploy/README.md`](deploy/README.md) for the full production runbook. In
short:

```bash
cd deploy
cp .env.example .env      # fill in real secrets
docker compose up -d
```

Services: `db`, `migrate`, `api`, `worker`, `web`. In production, add the
`tunnel` and `backup` profiles from `docker-compose.prod.yml`; nothing binds a
public port directly.

Before going live, confirm:

- [ ] `SESSION_SECRET` is 32+ random bytes, not the dev default
- [ ] `ENVIRONMENT=production` and `CORS_ALLOWED_ORIGIN` is https
- [ ] `FILE_STORAGE_PATH` points at a persistent, backed-up volume
- [ ] Database backups run **and one restore has actually been tested**
- [ ] `CONTROL_CORPUS_VERSION` matches the loaded corpus

### Known limitations at this stage

Stated plainly because a compliance tool should not be vague about its own gaps:

- **The audit trail is not a database table.** Authorization denials, admin
  access and login attempts go to stdout, are rotated at ~50 MB per service, and
  are not covered by the database backup. `finding_history` and report snapshots
  *are* durable.
- **No metrics, tracing, alerting or dashboards.** Health checks and structured
  logs only.
- **No encryption at rest**, and evidence files are excluded from backups — the
  `pg_dump` covers the database only.
- **Malware scanning is recorded, not enforced.** `malware_scan_status` defaults
  to `not_scanned` (ADR-012); upload validation covers size, MIME, magic bytes
  and content/extension agreement.
- **Single tenant, single framework, no connectors.** All deferred by design —
  see `docs/00_PRODUCT.md`.

---

## Troubleshooting

**`CORPUS_NOT_LOADED` when scoping an audit**
Run `.venv/bin/python -m app.corpus.loader`.

**"Image text extraction is not configured on this server"**
Tesseract is missing and no API provider is set. Install the binary or set
`OCR_PROVIDER` and `OCR_API_KEY`.

**Migrations fail on a fresh database**
The `pgvector` extension must exist. The compose `db` service supplies it; a
hand-rolled Postgres needs `CREATE EXTENSION vector;`.

**Everything evaluates to `INSUFFICIENT_EVIDENCE`**
Usually the worker is not running, so no facts have been extracted. Check the
worker process and each document's `extraction_status`.

**A control is missing from an audit's scope**
Check `applicability_status` on the scope row. `UNDETERMINED` means the company
profile does not answer a question that control's conditions ask — complete the
profile via `PATCH /api/audits/{id}` and re-run scoping. An unanswered question
never excludes a control silently.

**Production refuses to start**
`Settings.validate_for_environment` rejects dev defaults, a short
`SESSION_SECRET`, and an http CORS origin when `ENVIRONMENT=production`. The
error names the offending variable.

---

## Documentation

| File | Contents |
|---|---|
| [`docs/00_PRODUCT.md`](docs/00_PRODUCT.md) | Scope, level definitions, acceptance table |
| [`docs/01_REQUIREMENTS.md`](docs/01_REQUIREMENTS.md) | Behaviour, per feature, with forbidden behaviours |
| [`docs/02_ARCHITECTURE.md`](docs/02_ARCHITECTURE.md) | Components and boundaries |
| [`docs/03_DATA_MODEL.md`](docs/03_DATA_MODEL.md) | Every entity and constraint |
| [`docs/04_API_CONTRACT.md`](docs/04_API_CONTRACT.md) | Endpoints and error codes |
| [`docs/05_SECURITY.md`](docs/05_SECURITY.md) | Controls and the AI-safety test bar |
| [`docs/08_TESTING.md`](docs/08_TESTING.md) | Test strategy |
| [`docs/09_DEPLOYMENT.md`](docs/09_DEPLOYMENT.md) | Environment variables, runbook |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | 15 ADRs, with the reasoning and reversal cost |

API docs are served at `/docs` in development, and disabled in production.
