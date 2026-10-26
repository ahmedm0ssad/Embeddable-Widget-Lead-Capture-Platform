# FlyRank Backend AI — Agent Guide

## Entrypoints

| What | Command |
|------|---------|
| API server | `uvicorn app.main:app --reload --port 8000` |
| RQ worker (watches `ai-jobs`, `report-jobs`, `enrichment-jobs`) | `python -m app.core.worker` |
| Docker stack | `docker compose up --build` |
| Full test suite (excludes E2E) | `pytest` or the verbatim CI command below |

## CI pipeline (`.github/workflows/ci.yml` — source of truth)

Runs on `main` push/PR. Order: **isort → black → ruff → pytest**.

```yaml
# lint
isort --check-only --diff .
black --check --diff .
ruff check .

# ⚠️ This command must be kept byte-for-byte identical to the pytest
# invocation in .github/workflows/ci.yml. If they diverge, ci.yml is
# authoritative — treat any difference as a bug and fix AGENTS.md, not
# the other way around.
# test (exact CI path — use this to match CI)
python -m pytest \
  tests/embed/ \
  tests/leads/ \
  tests/widgets/ \
  tests/middleware/ \
  tests/models/ \
  tests/repositories/ \
  tests/routers/ \
  tests/scrapers/ \
  tests/services/ \
  tests/test_main.py \
  tests/test_background_jobs.py \
  tests/test_e2e_widget.py \
  tests/test_lead_worker.py \
  tests/test_report_worker.py \
  tests/test_worker.py \
  --ignore=tests/test_e2e.py \
  --ignore=tests/test_ai_e2e.py \
  --cov=app --cov-report=term-missing --tb=short -v
```

Single-file: `pytest tests/routers/test_ai.py -v` (works for any test path).

## Architecture

- **FastAPI** with `lifespan` pattern. Routers: `tasks`, `auth`, `scrape`, `ai`, `reports`, `widgets`, `embed`, `leads`.
- **3 RQ queues**: `ai-jobs`, `report-jobs`, `enrichment-jobs` — enqueued via `app.core.queue`. Worker runs all three.
- **Job state**: Redis hash `job:{id}` / `report_job:{id}` / `enrichment_job:{id}` — status lifecycle: `queued → started → finished/failed`.
- **Retry**: exponential backoff 10s → 60s → 300s, max 3, job timeout 600s.
- **Idempotency**: `Idempotency-Key` header → Redis `idempotency:{key}` with 24h TTL.
- **Database**: SQLite by default (no `DATABASE_URL` set). PostgreSQL via asyncpg when `DATABASE_URL` is provided. Postgres required for `scraped_books`, `widgets`, `leads`, `reports` tables (see `db/init.sql`).
- **Auth**: Supabase JWT. Require `SUPABASE_URL` + `SUPABASE_KEY` in `.env` — app refuses to start without them.
- **Middleware**: `BodyLimitMiddleware` (50KB), CORS (all origins, GET/POST/OPTIONS only).

## Testing quirks

- `conftest.py` sets `DATABASE_URL=""` and `REDIS_URL=""`, patches all `is_postgres_enabled` calls to `False`, and installs `_FakeRedis` + `_FakeQueue`. Tests are fully offline.
- `pyproject.toml`: `asyncio_mode = "auto"`.
- `tests/test_db_schema.py` is **ignored on pytest** (`addopts --ignore` in `pyproject.toml`) — needs a live Postgres. Run it explicitly (`pytest tests/test_db_schema.py`) against a running `db` service.
- `tests/repositories/test_postgres_lead_repo_live.py` is **ignored on pytest** (`addopts --ignore` in `pyproject.toml`) — a real asyncpg integration suite for `PostgresLeadRepository` that needs the compose `db` service. Run it explicitly: `python -m pytest tests/repositories/test_postgres_lead_repo_live.py -o addopts="" -v`. It reassigns `app.core.database.DATABASE_URL` at import (default `postgresql://flyrank:flyrank_pass@127.0.0.1:5432/flyrank`, override with `FLYRANK_TEST_DATABASE_URL`). This is the suite that caught F6 (asyncpg `IPv4Address` → `LeadResponse.ip_address: str`); keep it green whenever Postgres-facing code changes.
- E2E tests (`test_e2e.py`, `test_ai_e2e.py`) are **always excluded from CI** — need real Supabase/Redis/server.

## Framework quirks

- **Ruff**: per-file ignores for `B008` in `app/routers/auth.py`, `app/dependencies/auth.py`, `app/routers/reports.py`.
- **isort**: profile `black`.
- **Python 3.13** in CI (Docker is `python:3.13-slim`); `requires-python = ">=3.10"`.
- **Worker uses `SimpleWorker`** on Windows (`os.name == "nt"`), regular `Worker` on Linux.
- **Docker Redis**: mapped to host port **6380** (not 6379). Postgres on 5432.
- **Groq mock**: when `GROQ_API_KEY` is unset, `call_ai` returns `"Mock response to: {prompt}"`.

## Client IP / proxy trust (`TRUSTED_PROXY_CIDRS`)

- **One resolver drives everything**: `app/dependencies/client_ip.py::get_client_ip(request)` feeds the
  rate-limit keys (`app/dependencies/leads.py`), the fingerprint, the stored `lead.ip_address`, and
  therefore geo enrichment. Fix IP resolution once here and all side effects follow.
- **Secure by default**: `TRUSTED_PROXY_CIDRS` unset/empty → `X-Forwarded-For` is **ignored** and the
  direct TCP peer (`request.client.host`) is returned, preserving pre-F7 behavior. Do not trust the
  header without setting this.
- **When set** (comma-separated CIDRs, e.g. `10.0.0.0/8,172.16.0.0/12`): XFF is walked right-to-left,
  hops inside a trusted CIDR are skipped, the first untrusted hop is the client. **Operator contract:**
  every listed proxy MUST overwrite/strip incoming XFF from the untrusted client
  (`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`), or the header cannot be trusted.
- Never ship `172.18.0.0/16` (the docker bridge) as a production trust value — it does **not** sanitize
  headers. It is only used as a simulated trusted proxy in the M31 dev-verification live run.
- `docker compose` `app` service passes `.env` via `env_file`, so `TRUSTED_PROXY_CIDRS` set in `.env`
  flows into the container without a compose change.

## Pre-commit guardrail

This project has a history of unintended file bundling in fix commits (commits `2715feb` and `39e94a0` both swept up unrelated files via `git add -A`). **Before every commit**, run:

```bash
git status
git diff --cached --stat
```

Visually confirm that only intended files are staged. If binary docs, PDFs, or unrelated source files appear, unstage them with `git reset HEAD -- <path>` before committing.

## ⚠️ .env contains real credentials

`.env` has live `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_SERVICE_KEY`, and `GROQ_API_KEY`. **Do not commit `.env` or expose these values.**

## Health endpoint

- **`GET /health`** — Returns `{"status": "ok"}` with optional `redis` and `postgres` connectivity fields. Add `?` to any app for liveness/readiness checks. Docker Compose `app` service includes a HEALTHCHECK stanza (same pattern as `db`/`redis`). Dockerfile has `HEALTHCHECK` instruction.

## Tier C disposition

- **`POST /widgets` status code** — Plan originally specified `400` for validation errors; code returns `422` (FastAPI default). **RESOLVED (M17, plan update):** `422` is the correct semantic code for schema-validation failures, so the plan was corrected to `422` (see `docs/implementation-plan.md` M17 update) and the Tier C table in `docs/reviews/m1-architecture.md` records `STATUS: CLOSED — resolved via plan update (M17)`. No open Tier C items remain.
