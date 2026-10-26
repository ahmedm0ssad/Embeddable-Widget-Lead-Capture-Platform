# FlyRank Backend AI

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=flat&logo=redis&logoColor=white)](https://redis.io/)
[![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=white)](https://www.docker.com/)
[![pytest](https://img.shields.io/badge/tests-886%20passing-0A9EDC?style=flat&logo=pytest&logoColor=white)](https://pytest.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A production-ready FastAPI backend that combines task management, web scraping, Supabase authentication, asynchronous AI inference (RQ + Groq), automated PDF report generation, and an embeddable widget platform with spam-protected lead capture.

> **Capstone track?** See [`docs/capstone-submission.md`](docs/capstone-submission.md) for the widget & lead-capture mission deep-dive with runnable examples and eval results.

## Features

✅ FastAPI async REST API

✅ Task CRUD with SQLite / PostgreSQL persistence

✅ Automatic SQLite database creation

✅ Automatic table creation (`CREATE TABLE IF NOT EXISTS`)

✅ One-time database seeding (3 sample tasks, seeded only on first run)

✅ Parameterized SQL queries (SQL injection safe)

✅ Data survives restarts

✅ Supabase JWT authentication (signup, login, logout, protected endpoints)

✅ Robots.txt-compliant web scraper with rate limiting and retries

✅ Async AI inference via RQ (Redis Queue) + Groq, with mock fallback

✅ Job lifecycle management (`queued → started → finished/failed`)

✅ Exponential-backoff retries (10s → 60s → 300s, max 3, 600s timeout)

✅ Idempotent job creation via `Idempotency-Key` header

✅ Automated PDF report generation (ReportLab, served securely)

✅ Embeddable widget (JS bundle + public config endpoint)

✅ Lead capture with origin validation and 3-tier rate limiting

✅ Spam protection: honeypot trap, heuristic spam scoring, fingerprint dedup

✅ Geo enrichment of leads (multi-provider fallback chain)

✅ Fail-open webhook dispatch on submissions

✅ Lead dashboard: search, filter, sort, pagination, stats, CSV export

✅ Redis caching for widget config and lead statistics

✅ 50 KB request body limit middleware

✅ Comprehensive offline test suite (886+ passing tests, 99% coverage)

✅ Docker Compose stack and GitHub Actions CI pipeline

## Project Structure

```
app/
├── main.py                  # FastAPI app, lifespan, router mounting, /health
├── core/
│   ├── database.py          # asyncpg pool (Postgres) / SQLite fallback detection
│   ├── queue.py             # Redis connection, RQ queues, job CRUD (ai/report/enrichment)
│   ├── supabase.py          # Supabase client bootstrap
│   └── worker.py            # Standalone RQ worker entrypoint (3 queues)
├── dependencies/
│   ├── auth.py              # Bearer-token auth dependency
│   ├── embed.py             # Widget origin validation
│   ├── leads.py             # 3-tier rate limiting + origin checks
│   └── services.py          # DI providers (repositories, Redis)
├── middleware/
│   └── body_limit.py        # ASGI-level 50 KB payload cap
├── models/                  # Pydantic v2 schemas: task, auth, job, report, scraped_book, widget, lead
├── repositories/
│   ├── protocol.py          # Task / Widget / Lead repository Protocols
│   ├── sqlite_repo.py       # SQLite task repo (default backend)
│   ├── postgres_repo.py     # PostgreSQL task repo (asyncpg)
│   ├── widget_repo.py       # In-memory widget repo (dev/tests)
│   ├── lead_repo.py         # In-memory lead repo (dev/tests)
│   ├── postgres_widget_repo.py
│   ├── postgres_lead_repo.py
│   ├── scraped_book_repo.py # Scraped-book upserts (Postgres only)
│   └── report_repo.py       # Report CRUD (SQLite + Postgres)
├── routers/                 # tasks, auth, scrape, ai, reports, widgets, embed, leads
├── scrapers/
│   ├── session.py           # HTTP session, robots.txt parser, throttling, retries
│   ├── parser.py            # HTML parsing of listing + detail pages
│   ├── cleaner.py           # Field normalization (price, rating, availability)
│   └── pipeline.py          # Scrape orchestration (pages → books)
└── services/
    ├── task_service.py      # Task business logic
    ├── ai_service.py        # Groq inference call (mock fallback)
    ├── ai_worker.py         # RQ worker: AI job execution
    ├── report_service.py    # Report enqueue, metadata, DB aggregation
    ├── report_worker.py     # RQ worker: PDF generation
    ├── pdf_generator.py     # ReportLab document builder
    ├── widget_service.py    # Widget CRUD business logic
    ├── widget_js.py         # Widget JS bundle renderer
    ├── embed_service.py     # Public widget config lookup (Redis-cached)
    ├── lead_service.py      # Lead submission pipeline, stats, CSV export
    ├── lead_worker.py       # RQ worker: geo enrichment
    ├── spam_service.py      # Heuristic spam scoring
    ├── fingerprint_service.py # Submission fingerprint + dedup
    ├── geo_service.py       # IP geolocation (ipapi.co → ipinfo → ip-api)
    ├── webhook_service.py   # Fail-open webhook dispatch
    └── alert.py             # Failure alert stub (CRITICAL log)

tests/                       # ~1,000 unit + integration tests (fully offline)
db/init.sql                  # PostgreSQL DDL (6 tables + indexes)
scripts/seed_explain.py      # EXPLAIN ANALYZE index benchmark
.github/workflows/ci.yml     # isort → black → ruff → pytest pipeline
```

## Tech Stack

| Component       | Technology                                      |
|-----------------|-------------------------------------------------|
| Language        | Python ≥ 3.10 (3.13 in CI / Docker)             |
| Framework       | FastAPI + Uvicorn                               |
| Validation      | Pydantic v2                                     |
| Database        | SQLite (default) / PostgreSQL 16 (asyncpg)      |
| Cache / Queue   | Redis 7 + RQ                                    |
| AI Inference    | Groq SDK (default `llama-3.1-8b-instant`, mock fallback) |
| Authentication  | Supabase Auth (JWT)                             |
| PDF Generation  | ReportLab                                       |
| Scraping        | requests + urllib3 retries + BeautifulSoup4 + lxml |
| Testing         | pytest, pytest-asyncio, pytest-mock, httpx, pytest-cov |
| Tooling         | Ruff, Black, isort, python-dotenv               |
| Container       | Docker + Docker Compose                         |
| CI              | GitHub Actions                                  |

## Architecture

The API follows a strict layered design so each concern is isolated and independently testable:

```
Client
  ↓
FastAPI Router    — HTTP contract: validation, status codes, auth dependencies
  ↓
Service Layer     — business rules: seeding, job lifecycle, spam scoring, stats
  ↓
Repository Layer  — data access via Protocol-typed repositories (parameterized SQL)
  ↓
SQLite tasks.db / PostgreSQL
```

- **Routers** parse requests, enforce auth, and translate HTTP errors — no business logic.
- **Services** hold the business rules (e.g. the lead submission pipeline, job lifecycle).
- **Repositories** implement a shared Protocol (`app/repositories/protocol.py`) so the SQLite, PostgreSQL, and in-memory backends are interchangeable without touching upper layers.

### Background jobs

Long-running work (AI inference, PDF reports, lead enrichment) runs asynchronously on RQ:

1. `POST /ai`, `POST /reports`, or a lead submission enqueues a job on `ai-jobs`, `report-jobs`, or `enrichment-jobs`.
2. The API returns `202 Accepted` immediately with a `job_id`; state lives in Redis under `job:{id}` / `report_job:{id}` / `enrichment_job:{id}`.
3. A worker transitions the status `queued → started → finished/failed` and stores the result or error.
4. Failures retry with exponential backoff (10s → 60s → 300s, max 3) before the job is marked failed and an alert is raised.

### Widget submission pipeline

`POST /public/widget/{id}/submit` runs every submission through a defense-in-depth chain:

1. Widget lookup — unknown or inactive widgets return `404`.
2. Origin validation — the `Origin`/`Referer` host must match the widget's domain (wildcard `*.` supported); mismatches return `403`.
3. Rate limiting — three tiers (per-IP, per-widget/IP, per-widget) in a 60s window; excess returns `429` with `Retry-After`.
4. Honeypot trap — a hidden field that bots fill in; it flags the lead as spam (score `1.0`) and skips enrichment/webhooks.
5. Fingerprint dedup — identical submissions within the window return the existing lead instead of a duplicate.
6. Spam scoring — heuristic scoring (URLs, identical fields, non-ASCII avalanches, disposable email domains, malformed phones).
7. Storage + async side effects — the lead is stored, then geo enrichment is enqueued and any configured webhook is dispatched fire-and-forget.

## Database

### SQLite (`tasks.db` — default)

When no `DATABASE_URL` is set the app uses a local SQLite file (`tasks.db`):

- **Automatic creation** — the database file is created on first access.
- **Automatic table creation** — `CREATE TABLE IF NOT EXISTS tasks (...)` on startup.
- **Automatic seeding** — if the `tasks` table is empty, three sample tasks are inserted (Learn FastAPI, Write tests, Build a project).
- **Seed once** — seeding is guarded by a row count, so it never runs twice.
- **Persistence** — data survives restarts because every write is committed to the file.

### PostgreSQL

When `DATABASE_URL` is set, the app uses an asyncpg pool. The full schema — `tasks`, `scraped_books`, `reports`, `widgets`, `leads`, `rate_limits` — is defined in `db/init.sql` and applied automatically on first startup via Docker Compose.

## API Endpoints

### System

| Method | Path           | Auth | Description                                  |
|--------|----------------|------|----------------------------------------------|
| GET    | `/`            | No   | API info and Redis status                    |
| GET    | `/health`      | No   | Health check with Redis/Postgres status      |
| GET    | `/public/info` | No   | Public welcome message                       |

### Tasks

| Method | Path            | Auth | Description                              |
|--------|-----------------|------|------------------------------------------|
| GET    | `/tasks`        | No   | List tasks (`?search=&done=`)            |
| GET    | `/tasks/{id}`   | No   | Get a task by ID                         |
| POST   | `/tasks`        | No   | Create a task (`201`)                    |
| PUT    | `/tasks/{id}`   | No   | Update a task                            |
| DELETE | `/tasks/{id}`   | No   | Delete a task (`204`)                    |
| GET    | `/stats`        | No   | Task statistics (total / done / pending) |

### Auth

| Method | Path                   | Auth   | Description                           |
|--------|------------------------|--------|---------------------------------------|
| POST   | `/auth/signup`         | No     | Create an account (`201`)             |
| POST   | `/auth/login`          | No     | Sign in, returns access + refresh tokens |
| POST   | `/auth/logout`         | Bearer | Sign out (`204`)                      |
| GET    | `/protected/profile`   | Bearer | Current user profile                  |
| GET    | `/protected/dashboard` | Bearer | User dashboard                        |

### Scraper

| Method | Path        | Auth | Description                          |
|--------|-------------|------|--------------------------------------|
| POST   | `/scrape`   | No   | Scrape books (`?max_pages=5`) — Postgres required |

### AI / Background Jobs

| Method | Path             | Auth | Description                              |
|--------|------------------|------|------------------------------------------|
| POST   | `/ai`            | No   | Enqueue an AI inference job (`202`, optional `Idempotency-Key` header) |
| GET    | `/jobs/{job_id}` | No   | Poll job status                          |
| GET    | `/jobs`          | No   | List recent jobs (`?limit=&offset=`)     |

### Reports

| Method | Path                        | Auth | Description                          |
|--------|-----------------------------|------|--------------------------------------|
| POST   | `/reports`                  | No   | Enqueue a PDF report (`202`)         |
| GET    | `/reports/{job_id}`         | No   | Report status and download URL       |
| GET    | `/reports/files/{filename}` | No   | Download a generated PDF (path-traversal protected) |

### Widgets

| Method | Path                 | Auth   | Description                                        |
|--------|----------------------|--------|----------------------------------------------------|
| GET    | `/widgets`           | Bearer | List widgets (`?search=&active=&page=&page_size=`) |
| POST   | `/widgets`           | Bearer | Create a widget (`201`)                            |
| GET    | `/widgets/{widget_id}` | Bearer | Get a widget by ID                               |
| PUT    | `/widgets/{widget_id}` | Bearer | Update a widget (bumps `js_version`)             |
| DELETE | `/widgets/{widget_id}` | Bearer | Soft-delete a widget (`204`)                     |

### Public Widget Embed

| Method | Path                                   | Auth | Description                              |
|--------|----------------------------------------|------|------------------------------------------|
| GET    | `/public/widget/{id}/config`           | No   | Public widget config (JSON)              |
| GET    | `/public/widget/{id}/widget.js`        | No   | Embeddable widget JS bundle (1-year immutable cache) |
| POST   | `/public/widget/{id}/submit`           | No   | Submit a lead from the widget (`201`)    |

### Leads

| Method | Path                                          | Auth   | Description                              |
|--------|-----------------------------------------------|--------|------------------------------------------|
| GET    | `/widgets/{id}/leads`                         | Bearer | List widget leads (filters + pagination) |
| GET    | `/widgets/{id}/leads/{lead_id}`               | Bearer | Get a single lead                        |
| GET    | `/widgets/{id}/stats`                         | Bearer | Widget lead statistics                   |
| GET    | `/widgets/{id}/export`                        | Bearer | Export widget leads as CSV               |
| DELETE | `/widgets/{id}/leads/{lead_id}`               | Bearer | Delete a lead (`204`)                    |
| POST   | `/widgets/{id}/leads/batch-delete`            | Bearer | Batch delete leads (`204`)               |
| POST   | `/widgets/{id}/leads/{lead_id}/re-enrich`     | Bearer | Re-run geo enrichment (`202`)            |
| GET    | `/leads`                                      | Bearer | List leads across all widgets            |
| GET    | `/leads/stats`                                | Bearer | Global lead statistics                   |

## Example Requests

```bash
# Tasks — CRUD
curl http://localhost:8000/tasks
curl http://localhost:8000/tasks?search=fastapi&done=false
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Design review","done":false}'
curl -X PUT http://localhost:8000/tasks/4 \
  -H "Content-Type: application/json" \
  -d '{"title":"Design review","done":true}'
curl -X DELETE http://localhost:8000/tasks/4
curl http://localhost:8000/stats

# Auth
curl -X POST http://localhost:8000/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"pass123"}'
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"pass123"}'
# → {"access_token":"...","refresh_token":"..."}
curl http://localhost:8000/protected/profile \
  -H "Authorization: Bearer <access_token>"

# Enqueue an AI job (async)
curl -X POST http://localhost:8000/ai \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-unique-key" \
  -d '{"prompt":"Summarize FastAPI","model":"llama-3.1-8b-instant"}'
# → 202 {"job_id":"...","status":"queued","status_url":"/jobs/..."}

# Poll job status
curl http://localhost:8000/jobs/<job_id>

# Scrape books (Postgres required)
curl -X POST "http://localhost:8000/scrape?max_pages=3"

# Enqueue a PDF report
curl -X POST http://localhost:8000/reports
# → 202 {"job_id":"...","status":"queued"}

# Download the generated PDF
curl -o report.pdf http://localhost:8000/reports/files/report_<job_id>.pdf
```

## Running the Project

```bash
# 1. Clone the repository
git clone https://github.com/ahmedm0ssad/flyrank-backend.git
cd flyrank-backend

# 2. Install dependencies (Python 3.10+)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Required: SUPABASE_URL, SUPABASE_KEY (the app refuses to start without them)

# 4. Run the API
uvicorn app.main:app --reload --port 8000
```

The API is available at `http://localhost:8000` and interactive Swagger docs at `http://localhost:8000/docs`.

### Background worker

Async features (AI jobs, reports, lead enrichment) need Redis and a worker:

```bash
docker run -d -p 6380:6379 redis:7-alpine
python -m app.core.worker
```

### Full stack with Docker

```bash
docker compose up --build
```

This starts PostgreSQL 16 (with `db/init.sql` applied), Redis 7 on host port `6380`, and the app — each with healthchecks.

## Running Tests

The full test suite runs fully offline using in-memory repositories and fake Redis/RQ (no network, no Docker required):

```bash
pytest
```

With coverage:

```bash
pytest --cov=app --cov-report=term-missing
```

Exact CI invocation (from `.github/workflows/ci.yml`):

```bash
python -m pytest \
  tests/embed/ tests/leads/ tests/widgets/ tests/middleware/ tests/models/ \
  tests/repositories/ tests/routers/ tests/scrapers/ tests/services/ \
  tests/test_main.py tests/test_background_jobs.py tests/test_e2e_widget.py \
  tests/test_lead_worker.py tests/test_report_worker.py tests/test_worker.py \
  --ignore=tests/test_e2e.py --ignore=tests/test_ai_e2e.py \
  --cov=app --cov-report=term-missing --tb=short -v
```

Latest recorded CI result: **886 passed, 1 xfailed, 99% coverage** (2 pre-existing Postgres `ConnectionRefused` failures only occur when no Postgres is running). `tests/test_db_schema.py` requires a live Postgres, and `tests/test_e2e.py` / `tests/test_ai_e2e.py` require real Supabase/Redis — all three are excluded from CI.

Run a single test file:

```bash
pytest tests/routers/test_ai.py -v
```

## SQL Example

```sql
SELECT * FROM tasks WHERE done = 1;
```

Returns every task marked as complete. This is the exact query shape used by the repository layer (`app/repositories/sqlite_repo.py`) to compute task statistics — all queries are parameterized with `?` placeholders to prevent SQL injection.

## Database Screenshot

> Placeholder — add `docs/database.png` showing the `tasks` table populated with the seeded rows (e.g. from the SQLite CLI or a DB browser).

## Assignment Requirements Mapping

| Requirement (implementation-plan.md) | Status | Implementation |
|--------------------------------------|--------|----------------|
| M1 — Database schema & migrations (`widgets`, `leads`, `rate_limits`) | ✅ Implemented | `db/init.sql`, `app/core/database.py` |
| M2 — Widget CRUD: repository + service + router | ✅ Implemented | `app/repositories/{widget_repo,postgres_widget_repo}.py`, `app/services/widget_service.py`, `app/routers/widgets.py` |
| M3 — Public embed endpoints (config, widget.js) | ✅ Implemented | `app/routers/embed.py`, `app/services/{embed_service,widget_js}.py`, `app/dependencies/embed.py` |
| M4 — Lead capture submission pipeline | ✅ Implemented | `app/routers/leads.py`, `app/services/{lead_service,spam_service,fingerprint_service}.py`, `app/dependencies/leads.py`, `app/middleware/body_limit.py` |
| M5 — Geo enrichment background jobs | ✅ Implemented | `app/services/{geo_service,lead_worker}.py`, `app/core/queue.py` |
| M6 — Lead dashboard APIs (stats, export, batch ops) | ✅ Implemented | `app/routers/leads.py`, `app/services/lead_service.py`, `app/repositories/lead_repo.py` |
| M7 — Security hardening (origin edge cases, audit logging) | ✅ Implemented | `app/dependencies/embed.py`, `app/services/lead_service.py` (audit logger) |
| M8 — Testing completion & CI | ✅ Implemented | `tests/` (886+ passing tests), `.github/workflows/ci.yml` |

## Optional Features

- **Search** — tasks by title; widgets and leads by keyword
- **Filtering** — leads by status, spam score range, and date range; tasks by `done`
- **Sorting** — leads by any field, ascending or descending
- **Pagination** — widgets and leads (`page`, `page_size`, max 100)
- **Statistics** — task stats (`/stats`), per-widget and global lead stats
- **Timestamps** — `created_at` / `updated_at` on every model, auto-maintained
- **CSV export** — per-widget lead export with a 10,000-row cap (`X-Export-Truncated` header)
- **Caching** — Redis caching for widget config and lead stats (5-min TTL), 24h geo lookups
- **Idempotency** — `Idempotency-Key` header deduplicates AI job creation (24h TTL)
- **Webhooks** — optional per-widget HTTPS webhook, dispatched fire-and-forget
- **Protections** — honeypot trap, heuristic spam scoring, fingerprint dedup, 3-tier rate limiting, origin validation
- **Audit logging** — every submission outcome (success / blocked / spam) is logged as JSON

## Testing Strategy

- **Unit tests** — models, services, repositories, scrapers, and middleware in isolation; SQLite repos are exercised against a temporary database (`tmp_path`), so each run starts clean.
- **Integration tests** — router + service + repository flows through the FastAPI `TestClient`, covering the full widget submission pipeline and background-job lifecycle.
- **End-to-end tests** — `tests/test_e2e_widget.py` (offline, in CI) drives create-widget → submit-lead → enrichment; `tests/test_e2e.py` and `tests/test_ai_e2e.py` require live Supabase/Redis/server and are excluded from CI.
- **Offline fakes** — `tests/conftest.py` installs `_FakeRedis` and `_FakeQueue` and patches all `is_postgres_enabled` calls, so the suite never touches the network.
- **Repository swapping** — the Repository Protocol lets tests substitute in-memory or SQLite implementations for any data access layer.
- **Isolation** — every test resets fake state via autouse fixtures; no cross-test pollution.
- **Coverage** — CI enforces coverage reporting (`--cov=app --cov-report=term-missing`); latest run measures 99% statement coverage.

## Design Decisions

- **SQLite by default** — zero-config, file-based persistence for local development; PostgreSQL via `DATABASE_URL` when needed, selected at import time in `app/core/database.py`.
- **Repository pattern** — data access is behind Protocol-typed repositories (`app/repositories/protocol.py`), making SQLite/Postgres/in-memory backends swappable and trivially testable.
- **Parameterized queries** — all SQL uses `?` / `$n` placeholders; user input is never string-interpolated into queries.
- **Automatic initialization** — `tasks.db` and tables are created on first access; `db/init.sql` seeds the Postgres schema in Docker.
- **One-time seeding** — sample tasks are inserted only when the table is empty, so seed data never duplicates across restarts.
- **Async everywhere** — SQLite calls run in a threadpool (`run_in_threadpool`) so the event loop stays responsive.

## Future Improvements

- Email verification flow (currently relies on Supabase's default confirmation settings)
- Real alert delivery — replace the CRITICAL-log stub in `app/services/alert.py` with Slack/email/webhook
- Pagination and cursor-based listing for `/jobs` (currently Redis `SCAN`)
- Webhook retry queue with persistent delivery guarantees
- Postgres as the default configuration with SQLite as the dev fallback
- Persistent `rate_limits` audit table integration (schema exists in `db/init.sql`)
- Circuit breaker around geo providers and per-provider rate-limit budgets

## License

[MIT](LICENSE)
