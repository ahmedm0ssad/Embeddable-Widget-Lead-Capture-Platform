import asyncio
import csv
import io
import json
import logging
from datetime import date
from uuid import UUID

from fastapi import HTTPException, Request, status

from app.dependencies.client_ip import get_client_ip
from app.dependencies.leads import check_origin, check_rate_limits
from app.dependencies.services import get_lead_repo as _get_lead_repo
from app.dependencies.services import get_redis as _get_redis_provider
from app.models.lead import LeadResponse, LeadSubmit
from app.repositories.lead_repo import LeadRepository
from app.services import embed_service
from app.services.fingerprint_service import (
    check_dedup,
    compute_fingerprint,
    mark_seen,
)
from app.services.spam_service import score_submission
from app.services.webhook_service import dispatch_webhook

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("app.audit.submission")

MAX_EXPORT_ROWS = 10000

# Strong references to in-flight webhook tasks. asyncio only keeps weak
# references to tasks, so without this set a fire-and-forget task could be
# garbage-collected mid-execution and silently die; each task discards
# itself via add_done_callback once it completes.
_webhook_tasks: set[asyncio.Task] = set()


def _schedule_webhook(url: str, payload: dict) -> None:
    task = asyncio.create_task(dispatch_webhook(url, payload))
    _webhook_tasks.add(task)
    task.add_done_callback(_webhook_tasks.discard)


def _get_repo():
    return _get_lead_repo()


def _get_or_create_repo(repo: LeadRepository | None = None):
    if repo is not None:
        return repo
    return _get_lead_repo()


async def _get_redis():
    return await _get_redis_provider()


async def _cache_stats(cache_key: str, stats: dict, ttl: int = 300, redis=None):
    client = redis if redis is not None else await _get_redis()
    if client:
        try:
            await client.setex(cache_key, ttl, json.dumps(stats))
        except Exception:
            pass


async def _invalidate_stats_cache(
    widget_id: str | None = None,
    tenant_id: str | None = None,
    redis=None,
):
    client = redis if redis is not None else await _get_redis()
    if client:
        try:
            keys_to_delete = []
            if widget_id:
                keys_to_delete.append(f"stats:widget:{widget_id}")
            if tenant_id:
                keys_to_delete.append(f"stats:tenant:{tenant_id}")
            for key in keys_to_delete:
                await client.delete(key)
        except Exception:
            pass


_CACHE_TTL = 300


def _audit_log(
    outcome: str, widget_id: str, ip: str, extra: dict | None = None
) -> None:
    entry = {"outcome": outcome, "widget_id": widget_id, "ip": ip}
    if extra:
        entry.update(extra)
    audit_logger.info(json.dumps(entry))


async def submit_lead(
    widget_id: str,
    body: LeadSubmit,
    request: Request,
    repo: LeadRepository | None = None,
) -> tuple[LeadResponse, bool]:
    repo = _get_or_create_repo(repo)
    ip = get_client_ip(request)

    # Step 4: Widget exists & active check
    # Single lookup for both existence/active check and tenant_id extraction.
    # Embed service handles Redis caching internally.
    raw = await embed_service.get_raw_widget(widget_id)
    if raw is None or not raw.get("active", False):
        _audit_log("widget_not_found", widget_id, ip)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget not found",
        )

    widget_config = raw.get("config", {})
    config = {
        "widget_id": widget_id,
        "brand_color": widget_config.get("brand_color", "#2563eb"),
        "button_text": widget_config.get("button_text", "Get a Quote"),
        "fields": widget_config.get("fields", ["name", "email"]),
        "success_message": widget_config.get("success_message", "Thanks!"),
        "honeypot_field": widget_config.get("honeypot_field", "_hp_a3f9"),
    }
    tenant_id = raw.get("tenant_id", "unknown")

    # Step 5: Origin validation (reuses app/dependencies/embed.py)
    try:
        await check_origin(request, UUID(widget_id))
    except HTTPException:
        _audit_log("origin_rejected", widget_id, ip)
        raise

    # Step 6: Rate limit check - all 3 tiers
    retry_after = await check_rate_limits(ip, widget_id)
    if retry_after is not None:
        _audit_log("rate_limited", widget_id, ip, {"retry_after": retry_after})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    # Step 7: Honeypot check
    honeypot_field = config.get("honeypot_field", "")
    honeypot_triggered = False
    spam_score = 0.0
    spam_reasons: list[str] | None = None
    skip_enrich = False

    if honeypot_field and honeypot_field in body.form_data:
        hp_value = body.form_data.pop(honeypot_field, "")
        if hp_value:
            honeypot_triggered = True
            spam_score = 1.0
            spam_reasons = ["honeypot"]
            skip_enrich = True

    # Step 8: Fingerprint computation & dedup
    user_agent = request.headers.get("user-agent", "")
    referer = body.referer or request.headers.get("referer", "")

    fingerprint = compute_fingerprint(widget_id, ip, body.form_data)
    existing_lead_id = await check_dedup(fingerprint)
    if existing_lead_id is not None:
        existing = await repo.get_by_id(existing_lead_id)
        if existing is not None:
            _audit_log(
                "fingerprint_dedup",
                widget_id,
                ip,
                {
                    "fingerprint": fingerprint,
                    "existing_lead_id": str(existing.id),
                },
            )
            return existing, True

    # Step 9: Spam pre-check (skipped if honeypot already set)
    if not honeypot_triggered:
        spam_score, spam_reasons = score_submission(body.form_data)

    # Step 10: Insert lead
    lead = await repo.create(
        widget_id=widget_id,
        tenant_id=tenant_id,
        form_data=body.form_data,
        ip_address=ip,
        fingerprint=fingerprint,
        user_agent=user_agent,
        referer=referer,
        spam_score=spam_score,
        spam_reasons=spam_reasons,
        honeypot_triggered=honeypot_triggered,
    )

    # Mark fingerprint as seen
    await mark_seen(fingerprint, str(lead.id))

    # Invalidate stats cache best-effort
    await _invalidate_stats_cache(widget_id=widget_id, tenant_id=tenant_id)

    # Step 10b: Dispatch webhook (fire-and-forget; skipped for honeypot)
    # A successful submission must never be turned into an error by the
    # webhook, so the call is backgrounded and dispatch_webhook never
    # raises.
    webhook_url = widget_config.get("webhook_url") or ""
    if webhook_url and not honeypot_triggered:
        payload = {
            "lead_id": str(lead.id),
            "widget_id": widget_id,
            "form_data": body.form_data,
            "created_at": lead.created_at.isoformat(),
        }
        _schedule_webhook(webhook_url, payload)

    # Step 11: Enqueue enrichment job (skipped for honeypot)
    if not skip_enrich:
        try:
            from app.core.queue import create_enrichment_job

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, create_enrichment_job, str(lead.id))
        except Exception as exc:
            logger.warning(
                "Failed to enqueue enrichment job for lead %s: %s", lead.id, exc
            )

    if honeypot_triggered:
        _audit_log(
            "honeypot",
            widget_id,
            ip,
            {
                "fingerprint": fingerprint,
                "lead_id": str(lead.id),
            },
        )
    elif spam_score >= 0.5:
        _audit_log(
            "spam_flagged",
            widget_id,
            ip,
            {
                "fingerprint": fingerprint,
                "lead_id": str(lead.id),
                "spam_score": spam_score,
                "spam_reasons": spam_reasons,
            },
        )
    else:
        _audit_log(
            "success",
            widget_id,
            ip,
            {
                "fingerprint": fingerprint,
                "lead_id": str(lead.id),
            },
        )

    return lead, False


async def get_leads(
    widget_id: str,
    tenant_id: str,
    include_honeypot: bool = False,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    status: str | None = None,
    spam_min: float | None = None,
    spam_max: float | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    repo: LeadRepository | None = None,
) -> tuple[list[LeadResponse], int]:
    repo = _get_or_create_repo(repo)
    return await repo.list_by_widget(
        widget_id=widget_id,
        tenant_id=tenant_id,
        include_honeypot=include_honeypot,
        page=page,
        page_size=page_size,
        search=search,
        status=status,
        spam_min=spam_min,
        spam_max=spam_max,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )


async def get_all_leads(
    tenant_id: str,
    include_honeypot: bool = False,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    status: str | None = None,
    spam_min: float | None = None,
    spam_max: float | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    repo: LeadRepository | None = None,
) -> tuple[list[LeadResponse], int]:
    repo = _get_or_create_repo(repo)
    return await repo.list_by_tenant(
        tenant_id=tenant_id,
        include_honeypot=include_honeypot,
        page=page,
        page_size=page_size,
        search=search,
        status=status,
        spam_min=spam_min,
        spam_max=spam_max,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )


async def get_lead_detail(
    lead_id: str,
    widget_id: str,
    tenant_id: str,
    repo: LeadRepository | None = None,
) -> LeadResponse | None:
    repo = _get_or_create_repo(repo)
    lead = await repo.get_by_id(lead_id)
    if lead is None:
        return None
    if str(lead.widget_id) != widget_id or str(lead.tenant_id) != tenant_id:
        return None
    return lead


async def get_widget_stats(
    widget_id: str,
    tenant_id: str,
    skip_cache: bool = False,
    repo: LeadRepository | None = None,
    redis=None,
) -> dict:
    client = redis if redis is not None else await _get_redis()
    cache_key = f"stats:widget:{widget_id}"

    if client and not skip_cache:
        try:
            import json

            cached = await client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    repo = _get_or_create_repo(repo)
    stats = await repo.get_stats(widget_id, tenant_id)

    if client:
        await _cache_stats(cache_key, stats, redis=redis)
    return stats


async def get_tenant_stats(
    tenant_id: str,
    skip_cache: bool = False,
    repo: LeadRepository | None = None,
    redis=None,
) -> dict:
    client = redis if redis is not None else await _get_redis()
    cache_key = f"stats:tenant:{tenant_id}"

    if client and not skip_cache:
        try:
            import json

            cached = await client.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    repo = _get_or_create_repo(repo)
    stats = await repo.get_tenant_stats(tenant_id)

    if client:
        await _cache_stats(cache_key, stats, redis=redis)
    return stats


async def export_csv(
    widget_id: str,
    tenant_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    repo: LeadRepository | None = None,
) -> tuple[str, bool]:
    repo = _get_or_create_repo(repo)
    leads = await repo.get_export_data(
        widget_id=widget_id,
        tenant_id=tenant_id,
        date_from=date_from,
        date_to=date_to,
    )
    truncated = len(leads) > MAX_EXPORT_ROWS
    leads = leads[:MAX_EXPORT_ROWS]

    output = io.StringIO()
    writer = csv.writer(output)

    fields = [
        "id",
        "widget_id",
        "tenant_id",
        "form_data",
        "ip_address",
        "user_agent",
        "referer",
        "fingerprint",
        "geo_country",
        "geo_city",
        "geo_region",
        "geo_isp",
        "geo_provider",
        "spam_score",
        "spam_reasons",
        "honeypot_triggered",
        "status",
        "created_at",
        "updated_at",
    ]
    writer.writerow(fields)

    for lead in leads:
        row = []
        for f in fields:
            val = lead.get(f)
            if isinstance(val, (dict, list)):
                import json

                val = json.dumps(val)
            elif val is None:
                val = ""
            elif isinstance(val, bool):
                val = str(val).lower()
            else:
                val = str(val)
            row.append(val)
        writer.writerow(row)

    return output.getvalue(), truncated


async def delete_lead(
    lead_id: str, widget_id: str, tenant_id: str, repo: LeadRepository | None = None
) -> bool:
    repo = _get_or_create_repo(repo)
    result = await repo.delete(lead_id, widget_id, tenant_id)
    if result:
        await _invalidate_stats_cache(widget_id=widget_id, tenant_id=tenant_id)
    return result


async def batch_delete_leads(
    lead_ids: list[str],
    widget_id: str,
    tenant_id: str,
    repo: LeadRepository | None = None,
) -> int:
    repo = _get_or_create_repo(repo)
    count = await repo.batch_delete(lead_ids, widget_id, tenant_id)
    if count > 0:
        await _invalidate_stats_cache(widget_id=widget_id, tenant_id=tenant_id)
    return count


async def re_enrich_lead(
    lead_id: str,
    widget_id: str,
    tenant_id: str,
    repo: LeadRepository | None = None,
) -> dict:
    from app.core.queue import create_enrichment_job, get_enrichment_active

    repo = _get_or_create_repo(repo)
    lead = await repo.get_by_id(lead_id)
    if lead is None or str(lead.widget_id) != widget_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found",
        )

    loop = asyncio.get_event_loop()
    active_job_id = await loop.run_in_executor(None, get_enrichment_active, lead_id)
    if active_job_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Lead already has an active enrichment job ({active_job_id})",
        )

    if lead.status in ("enriched", "pending"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Lead status is '{lead.status}', can only re-enrich 'failed' leads",
        )

    await loop.run_in_executor(None, create_enrichment_job, lead_id)
    return {"status": "re-enqueued", "lead_id": lead_id}
