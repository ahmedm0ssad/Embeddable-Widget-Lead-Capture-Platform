# BUILDLOG.md — AI usage log

This log is part of the five-file submission pack (§11). Its job is honesty:
where AI helped, where it was wrong, and what I changed. Perfection is not
graded — honesty is.

## How AI was used

AI assistance was used throughout this capstone's development, and the record
is visible in the git history. The pattern was always: **AI proposes, I
review, test, and own the result.** The full offline suite (938 tests, 100%
coverage) and the CI pipeline (`isort → black → ruff → pytest`) were the
safety net that caught AI mistakes before they shipped.

### Phase 1 — Design (docs/implementation-plan.md)
- AI helped draft the milestone plan (M1–M8): data model, API contracts for
  the three request paths (owner / customer-site / visitor), and the tiered
  security approach. I reviewed and re-tiered items myself (several were
  reclassified Tier C and tracked in `docs/reviews/m1-architecture.md`).

### Phase 2–3 — Build (milestones M1–M23)
- **Widget CRUD + embed delivery:** AI generated the repository/service/router
  scaffolding; I added the tenant-scoping review and the versioned-bundle
  cache strategy.
- **Hardened submission path:** AI drafted the validation model, the 3-tier
  rate limiter, the honeypot + heuristic spam scorer, the fingerprint dedup,
  and the geo fallback chain. I wrote the "attacker" tests that probe each
  layer (flood → 429, honeypot → fake success, providers down → still 201).
- **Side effects:** AI produced the fail-open webhook dispatch; I discovered
  and fixed a real bug it introduced (see below).
- **Dashboard:** AI generated the stats/export/CRUD endpoints; I reviewed the
  aggregation SQL and the CSV export caps.

### What AI got wrong — and what I changed

These are the cases where the first AI answer was wrong or incomplete; each
has a fixing commit and a regression test.

| AI mistake | What I changed | Fix commit |
|---|---|---|
| Fire-and-forget webhook task could be garbage-collected mid-flight (asyncio holds only weak refs to tasks) | Added `_webhook_tasks` set with `add_done_callback` discard so in-flight tasks stay alive | `ea5c738` |
| Re-enrich endpoint had a race: two rapid calls could queue two enrichment jobs for the same lead | Added a Redis `lead_id → job_id` active key + `409 Conflict` on overlap | `d6fb7c3` |
| Unbounded in-process fallback dict could grow without limit when Redis was down | Bounded with `OrderedDict` + LRU eviction (`_MAX_IN_PROCESS_KEYS`) | `a7f9ed2`, `7f58e08`, `2f6ce6a` |
| Global singleton repositories/services made DI overrides and per-request state awkward | Replaced with `Depends()`-based DI providers (`get_lead_repo`/`get_widget_repo`/`get_redis`) | `9cb26b5`, `6585a96` |
| FastAPI submit route broke across version range | Parsed the submit body manually (content-type check + JSON decode) instead of relying on a Pydantic body param | `d55be76`, `aa01f18` |
| Initial plan said validation errors return `400`; the code returns `422` (FastAPI default) | Kept `422`, updated the plan; flagged in the review tracker for sign-off | `85ba47d` |
| **Cache-Control header on the config endpoint was missing entirely** — the DoD requires "correct HTTP cache headers" | Added `Cache-Control: public, max-age=300` and removed the xfail marker | this branch |

### AI cost & grounding

- **Cost:** the track's AI calls (Groq) go through `app/services/ai_service.py`
  with a mock fallback when `GROQ_API_KEY` is unset; the test suite runs fully
  offline with fakes, so CI costs nothing.
- **Grounding:** every AI-generated claim in this repo is backed by a test
  name + output in `EVIDENCE.md`. The evaluator can reproduce any proof with
  the single `test:` command in `capstone.yaml`.

### What AI helped with in this submission-pack branch

- Drafting `capstone.yaml`, `EVIDENCE.md`, `BUILDLOG.md`, the demo seed script
  (`scripts/seed_demo.py`), and the second-origin page
  (`customer-site/index.html`).
- Capturing and organizing the real test output into per-checkbox proofs.
- Detecting the missing Cache-Control header while gathering evidence, and
  fixing it.

### What I changed after AI produced the above

- Wrote `scripts/seed_demo.py` to write through the real repository layer
  (idempotent; refuses to run without Postgres so the seeded demo persists).
- Made the customer-site page widget-id-driven (`?widget=<id>`) so one static
  file works for any widget.
- Fixed the Cache-Control gap and re-ran the full CI suite to 938 passed /
  100% coverage before pasting any evidence.
