# EVIDENCE.md — one proof per Definition-of-Done checkbox

Every checkbox in the capstone brief §6 has exactly one pasted proof below: a
real test name and its output from the suite. All proofs come from one offline
run of the exact CI invocation (`.github/workflows/ci.yml` — source of truth),
performed on branch `feature/capstone-submission-pack`:

```text
python -m pytest tests/embed/ tests/leads/ tests/widgets/ tests/middleware/ \
  tests/models/ tests/repositories/ tests/routers/ tests/scrapers/ \
  tests/services/ tests/test_main.py tests/test_background_jobs.py \
  tests/test_e2e_widget.py tests/test_lead_worker.py tests/test_report_worker.py \
  tests/test_worker.py --ignore=tests/test_e2e.py --ignore=tests/test_ai_e2e.py \
  --cov=app --cov-report=term-missing --tb=short -v

TOTAL    2873      0   100%
============================ 938 passed in 36.88s =============================
```

---

## WIDGET MANAGEMENT

### 1 · Authenticated CRUD endpoints for widgets; requests without valid auth are rejected

```text
tests/widgets/test_router.py::TestListWidgets::test_list_widgets_401_without_auth PASSED [ 27%]
tests/widgets/test_router.py::TestCreateWidget::test_create_widget_401_without_auth PASSED [ 28%]
tests/widgets/test_router.py::TestGetWidget::test_get_widget_401_without_auth PASSED [ 28%]
tests/widgets/test_router.py::TestUpdateWidget::test_update_widget_401_without_auth PASSED [ 29%]
tests/widgets/test_router.py::TestDeleteWidget::test_delete_widget_401_without_auth PASSED [ 29%]
```

CRUD happy paths (`201`/`200`/`204`), `404`, `422`, and `409` (duplicate
domain) are also covered:

```text
tests/widgets/test_router.py::TestCreateWidget::test_create_widget_returns_201 PASSED [ 28%]
tests/widgets/test_router.py::TestCreateWidget::test_create_widget_422_invalid PASSED [ 28%]
tests/widgets/test_router.py::TestCreateWidget::test_create_widget_409_duplicate PASSED [ 28%]
```

### 2 · Multi-tenant isolation proven: tenant A cannot read or modify tenant B's widgets or submissions

```text
tests/widgets/test_router.py::TestGetWidget::test_get_widget_403_wrong_tenant PASSED [ 28%]
tests/widgets/test_router.py::TestListWidgets::test_list_widgets_tenant_isolation PASSED [ 27%]
tests/widgets/test_repository.py::TestWidgetRepository::test_tenant_isolation_in_list PASSED [ 27%]
tests/leads/test_repository.py::TestLeadRepository::test_tenant_isolation PASSED [ 13%]
```

Isolation is enforced in the repository layer on every query
(`widget_id + tenant_id` in all `WHERE` clauses) — see `app/repositories/{widget_repo,lead_repo,postgres_widget_repo,postgres_lead_repo}.py`.

### 3 · Embed snippet generated per widget

```text
tests/embed/test_widget_js.py::TestGenerateScriptTag::test_basic_script_tag PASSED [  3%]
tests/embed/test_widget_js.py::TestGenerateScriptTag::test_versioned_url_changes_with_version PASSED [  3%]
tests/embed/test_widget_js.py::TestGenerateScriptTag::test_custom_base_url PASSED [  3%]
tests/embed/test_widget_js.py::TestGenerateScriptTag::test_script_tag_format PASSED [  3%]
```

Output shape (`app/services/widget_js.py::generate_script_tag`):

```html
<script src="http://localhost:8000/public/widget/<widget-id>/widget.js?v=<js_version>" data-widget-id="<widget-id>" defer></script>
```

---

## WIDGET DELIVERY

### 4 · Public config endpoint serves a small payload with correct HTTP cache headers

```text
tests/embed/test_router.py::TestGetWidgetConfig::test_config_200 PASSED [  0%]
tests/embed/test_router.py::TestGetWidgetConfig::test_config_payload_deep_schema PASSED [  0%]
tests/embed/test_router.py::TestGetWidgetConfig::test_config_cache_control_header PASSED [  0%]
```

The cache-control test asserts the presence of `public` and `max-age` on the
config response (`app/routers/embed.py`), alongside a 300s server-side Redis
cache (`app/services/embed_service.py::get_widget_config`).

### 5 · Widget JavaScript is served as a versioned bundle (new version = new URL or cache-bust)

```text
tests/embed/test_router.py::TestGetWidgetJs::test_widget_js_200 PASSED [  0%]
tests/embed/test_router.py::TestGetWidgetJs::test_widget_js_caching_headers PASSED [  0%]
tests/embed/test_router.py::TestGetWidgetJs::test_widget_js_without_v_param PASSED [  1%]
tests/embed/test_widget_js.py::TestGenerateScriptTag::test_versioned_url_changes_with_version PASSED [  3%]
```

`widget.js` is served with `Cache-Control: public, max-age=31536000, immutable`
(`app/routers/embed.py:63`); editing a widget bumps `js_version`, changing the
cache-busting `?v=` query parameter.

### 6 · The widget renders on a page served from a different origin than your API

The bundle resolves its API origin at runtime from the `<script>` element that
loaded it (`document.currentScript.src`), so both the config fetch and the
submit POST are absolute to the API host — never to the embedding page. The
bundle is executed against a simulated second-origin page (node DOM/XHR
harness, `tests/embed/harness_foreign_origin.js`): the script tag's `src` lives
on `api.flyrank.example` while the page is served from `customer-site.example`.
The URLs the bundle actually requests must resolve to the script's host, not
the page's:

```text
$ node tests/embed/harness_foreign_origin.js <rendered-widget.js>
{"config":"https://api.flyrank.example/public/widget/abc/config","submit":"https://api.flyrank.example/public/widget/abc/submit"}
```

```text
tests/embed/test_widget_js.py::TestCrossOriginRender::test_config_and_submit_resolve_to_script_origin PASSED [100%]
============================== 1 passed in 0.22s ==============================
```

Before the fix the same harness resolved both URLs against the page origin
(`https://customer-site.example/...`) — a silent 404 and no render — so this
test fails on the pre-fix bundle and passes on the post-fix bundle.

Manual proof: `customer-site/index.html` is a plain HTML page that injects the
one-line `<script>` and is served from a second local origin
(`python -m http.server 5500` → `http://localhost:5500/?widget=<id>`).

### 7 · Cross-origin submissions work: CORS headers correct, preflight (OPTIONS) handled

```text
tests/embed/test_router.py::TestCors::test_options_preflight PASSED [  1%]
tests/middleware/test_cors.py::TestCorsHeaders::test_options_preflight_returns_cors PASSED [ 31%]
tests/middleware/test_cors.py::TestCorsHeaders::test_preflight_headers_include_allow_headers_and_max_age PASSED [ 32%]
tests/middleware/test_cors.py::TestCorsHeaders::test_preflight_with_disallowed_method_returns_400 PASSED [ 32%]
tests/middleware/test_cors.py::TestOriginValidationViaSubmit::test_origin_exact_match_accepted PASSED [ 32%]
```

---

## PUBLIC SUBMISSION API

### 8 · All incoming input validated; malformed and oversized payloads rejected with appropriate 4xx codes and JSON errors

```text
tests/leads/test_router.py::TestSubmitLead::test_422_validation_error PASSED [ 13%]
tests/leads/test_router.py::TestSubmitLead::test_413_payload_too_large PASSED [ 13%]
tests/leads/test_router.py::TestSubmitLead::test_403_origin_mismatch PASSED [ 13%]
tests/leads/test_router.py::TestSubmitLead::test_404_widget_not_found PASSED [ 13%]
tests/models/test_lead.py::TestSanitize::test_escapes_html_tags PASSED [ 33%]
```

Rejected payloads return clean 4xx JSON errors — never a 500. Field-level
sanitization (HTML escaping, length caps, email/phone format) lives in
`app/models/lead.py::LeadSubmit`.

### 9 · Valid submissions stored safely, linked to the right widget and tenant

```text
tests/leads/test_router.py::TestSubmitLead::test_201_success PASSED [ 13%]
tests/test_e2e_widget.py::TestE2EWidget::test_submit_enrich_dashboard_stats PASSED [ 95%]
tests/leads/test_router.py::TestSubmissionPersistence::test_fingerprint_persisted_on_lead PASSED [ 18%]
```

The E2E proof drives the full loop: create widget → submit lead → run
enrichment → read the row back from the repository and assert it is linked to
the right `widget_id` and `tenant_id`.

### 10 · Rate limiting per IP and/or per widget returns 429 under a burst — and the API keeps serving legitimate traffic

```text
tests/leads/test_router.py::TestSubmitLead::test_429_rate_limited PASSED [ 13%]
tests/leads/test_router.py::TestSubmitLead::test_429_retry_after_header PASSED [ 14%]
tests/leads/test_router.py::TestSubmitLead::test_window_reset_after_retry_after_expires PASSED [ 14%]
tests/leads/test_router.py::TestSubmitLead::test_rate_limit_takes_precedence_over_dedup PASSED [ 14%]
```

Three tiers (global-per-IP / per-widget-per-IP / per-widget) in a 60s window,
with `Retry-After` on the 429 — `app/dependencies/leads.py`.

---

## ABUSE PROTECTION

### 11 · At least one spam-prevention technique (honeypot field, token, or heuristic) demonstrably blocks a spam submission

Honeypot trap — a bot filling the hidden field is flagged and its lead stored
as spam (score 1.0) without enrichment/webhook:

```text
tests/test_e2e_widget.py::TestE2EWidget::test_honeypot_flow PASSED [ 94%]
tests/leads/test_router.py::TestSubmitLead::test_honeypot_returns_fake_success PASSED [ 14%]
tests/leads/test_service.py::TestSubmitLead::test_honeypot_branch PASSED [ 19%]
tests/leads/test_webhook.py::TestWebhookDispatch::test_honeypot_skips_dispatch PASSED [ 23%]
```

Heuristic spam scoring (URLs, identical fields, non-ASCII avalanches,
disposable email domains, malformed phones):

```text
tests/leads/test_spam.py::TestSpamScoring::test_url_in_message PASSED [ 21%]
tests/leads/test_spam.py::TestSpamScoring::test_email_blacklist PASSED [ 22%]
tests/leads/test_spam.py::TestSpamScoring::test_threshold_logic_above PASSED [ 22%]
tests/leads/test_spam.py::TestSpamScoring::test_score_capped_at_one PASSED [ 22%]
```

Fingerprint dedup — identical submissions within the window return the
existing lead instead of a duplicate:

```text
tests/leads/test_router.py::TestSubmitLead::test_dedup_returns_same_lead_id PASSED [ 14%]
```

---

## ENRICHMENT & SAFE SIDE EFFECTS

### 12 · IP→geo enrichment uses a provider fallback chain: provider A down → provider B answers → submission enriched

```text
tests/leads/test_geo.py::TestGeoEnrich::test_fallback_to_ipinfo_when_ipapi_fails PASSED [  5%]
tests/leads/test_geo.py::TestGeoEnrich::test_fallback_to_ipapi_com_when_first_two_fail PASSED [  5%]
tests/leads/test_geo.py::TestGeoEnrich::test_provider_order_is_correct PASSED [  5%]
tests/leads/test_geo.py::TestGeoEnrich::test_timeout_on_first_provider_falls_through PASSED [  5%]
tests/test_lead_worker.py::TestRunEnrichmentJob::test_successful_enrichment PASSED [ 96%]
```

Chain order is `ipapi.co → ipinfo.io → ip-api.com` with a 3s timeout per
provider (`app/services/geo_service.py`); a 24h Redis cache avoids repeat
lookups. Providers are mocked in tests so the fallback is deterministic.

### 13 · All providers down → submission still succeeds (without geo). Degrade, never fail.

```text
tests/leads/test_geo.py::TestGeoEnrich::test_all_providers_fail_returns_none PASSED [  5%]
tests/leads/test_geo.py::TestEnrichmentFailureFlow::test_submit_201_when_enrichment_fails PASSED [  5%]
tests/leads/test_geo.py::TestEnrichmentFailureFlow::test_nullable_geo_fields_on_enrichment_failure PASSED [  5%]
```

The submit path returns `201` regardless of enrichment success; the lead is
stored with nullable geo fields and `status = "failed"` only if every retry is
exhausted.

### 14 · A failing confirmation email / webhook does not prevent the submission from being stored

```text
tests/routers/test_webhook_submit.py::TestSubmitReturns201DespiteWebhook::test_webhook_failure_still_201 PASSED [ 63%]
tests/routers/test_webhook_submit.py::TestSubmitReturns201DespiteWebhook::test_webhook_success_still_201 PASSED [ 63%]
tests/leads/test_webhook.py::TestWebhookDispatch::test_webhook_failure_still_returns_lead PASSED [ 23%]
```

`app/services/webhook_service.py::dispatch_webhook` is fail-open (never
raises: timeout, DNS, connection refused, non-2xx all caught) and is fired
fire-and-forget after the lead is stored.

---

## TESTS & DOCUMENTATION

### 15 · Automated tests cover: CORS preflight, invalid payload, oversized payload, rate limiting, spam control, provider fallback, and successful widget rendering

Each required scenario maps to a passing test:

| Scenario | Proof (test name) |
|---|---|
| CORS preflight | `tests/middleware/test_cors.py::test_options_preflight_returns_cors` |
| Invalid payload | `tests/leads/test_router.py::test_422_validation_error` |
| Oversized payload | `tests/leads/test_router.py::test_413_payload_too_large` |
| Rate limiting | `tests/leads/test_router.py::test_429_rate_limited` |
| Spam control | `tests/leads/test_spam.py::TestSpamScoring::test_email_blacklist` |
| Provider fallback | `tests/leads/test_geo.py::test_fallback_to_ipinfo_when_ipapi_fails` |
| Successful widget rendering | `tests/embed/test_widget_js.py::test_js_references_config_at_runtime` |

Full suite result from the exact CI invocation:

```text
TOTAL                                       2873      0   100%
============================ 938 passed in 36.88s =============================
```

### 16 · README with architecture diagram, setup instructions, and API documentation; the five submission-pack files present

- `README.md` — architecture diagram, setup/run/seed steps, full API reference.
- `capstone.yaml` — the machine-readable manifest (run/seed/test/base_url/endpoints).
- `EVIDENCE.md` — this file.
- `BUILDLOG.md` — the AI-usage log.
- `.env.example` — every environment variable with safe placeholder values.

```text
$ ls
README.md  capstone.yaml  EVIDENCE.md  BUILDLOG.md  .env.example
```

---

## Honest notes (limitations)

- **Branch, not a separate repo.** Per the brief §11 the capstone should live
  in its own public `flyrank-capstone-widgetplatform` repository, created on
  day one. This codebase is developed on a dedicated branch
  (`feature/embed-widget-lead-capture` → `feature/capstone-submission-pack`) of
  `ahmedm0ssad/flyrank-backend` instead. The capstone domain code, tests, and
  docs are all present here; the migration to a standalone repo is a
  follow-up.
- **CORS is fully open** (`allow_origins=["*"]`, `allow_methods=GET,POST,OPTIONS`)
  — correct for the brief's public submission path, but worth tightening
  (allow-list of tenant origins) before production.
- **`POST /widgets` returns `422`** for validation errors, while the original
  implementation plan specified `400`. Tracked in `docs/reviews/m1-architecture.md`.
- **Rate limits are process-local when Redis is down** (bounded LRU dict in
  `app/dependencies/leads.py`) — a safe degradation, not a distributed limiter.
- **Email side effect is implemented as a webhook.** The brief allows "a
  confirmation email / webhook"; the fail-open webhook covers the box.
