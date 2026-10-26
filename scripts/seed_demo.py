"""Seed demo data for the Embeddable Widget & Lead-Capture Platform.

Requires PostgreSQL: run ``docker compose up --build`` first, then::

    python -m scripts.seed_demo

The script is idempotent — re-running it will not duplicate the demo widgets
or leads. It writes through the same repository layer the API uses (no
HTTP/auth required), creates a demo tenant, two widgets on different
domains, and a handful of sample leads, then prints the generated embed
snippets and a demo curl transcript.
"""

import asyncio
import sys

from app.services.fingerprint_service import compute_fingerprint

DEMO_TENANT_ID = "11111111-2222-3333-4444-555555555555"
DEMO_HONEYPOT_FIELD = "_hp_demo42"

WIDGET_SPECS = [
    {
        "name": "Demo Signup Form",
        "domain": "https://myshop.com",
        "config": {
            "brand_color": "#2563eb",
            "button_text": "Get a Quote",
            "fields": ["name", "email", "phone"],
            "success_message": "Thanks! We'll be in touch.",
            "honeypot_field": DEMO_HONEYPOT_FIELD,
        },
        "leads": [
            {
                "form_data": {
                    "name": "Ana Torres",
                    "email": "ana@example.com",
                    "phone": "+15551234567",
                },
                "ip_address": "203.0.113.7",
                "geo": {
                    "geo_country": "United States",
                    "geo_city": "Austin",
                    "geo_region": "Texas",
                    "geo_isp": "ExampleNet LLC",
                    "geo_provider": "ipapi",
                },
            },
            {
                "form_data": {
                    "name": "Liam Chen",
                    "email": "liam@example.com",
                    "phone": "+15559876543",
                },
                "ip_address": "198.51.100.23",
                "geo": {
                    "geo_country": "United States",
                    "geo_city": "Seattle",
                    "geo_region": "Washington",
                    "geo_isp": "ExampleHosting",
                    "geo_provider": "ipinfo",
                },
            },
            {
                "form_data": {
                    "name": "Mia Okafor",
                    "email": "mia@example.com",
                    "phone": "+15555550101",
                },
                "ip_address": "192.0.2.44",
                "geo": {
                    "geo_country": "United Kingdom",
                    "geo_city": "London",
                    "geo_region": "England",
                    "geo_isp": "ExampleBroadband",
                    "geo_provider": "ip-api",
                },
            },
            {
                "form_data": {"name": "Noah Silva", "email": "noah@example.com"},
                "ip_address": "203.0.113.99",
                "geo": None,
            },
            {
                "form_data": {
                    "name": "Spam Bot",
                    "email": "spam@mailinator.com",
                    "phone": "not-a-phone",
                },
                "ip_address": "198.51.100.200",
                "spam_score": 0.7,
                "spam_reasons": ["disposable_email_domain", "phone_pattern_mismatch"],
                "geo": None,
            },
            {
                "form_data": {
                    "name": "Bot",
                    "email": "bot@example.com",
                    DEMO_HONEYPOT_FIELD: "filled by bot",
                },
                "ip_address": "192.0.2.250",
                "honeypot_triggered": True,
                "spam_score": 1.0,
                "spam_reasons": ["honeypot"],
                "geo": None,
            },
        ],
    },
    {
        "name": "Demo CTA Popover",
        "domain": "https://marketing.landingpage.io",
        "config": {
            "brand_color": "#0ea5e9",
            "button_text": "Claim Your Offer",
            "fields": ["name", "email"],
            "success_message": "Offer claimed! Check your inbox.",
            "honeypot_field": DEMO_HONEYPOT_FIELD,
        },
        "leads": [
            {
                "form_data": {"name": "Sofia Rossi", "email": "sofia@example.com"},
                "ip_address": "203.0.113.150",
                "geo": {
                    "geo_country": "Italy",
                    "geo_city": "Milan",
                    "geo_region": "Lombardy",
                    "geo_isp": "ExampleTelecom",
                    "geo_provider": "ipapi",
                },
            }
        ],
    },
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


async def _check_connection() -> bool:
    import asyncpg

    from app.core.database import get_pool

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return True
    except (OSError, asyncpg.PostgresError) as exc:
        print(
            f"Could not reach PostgreSQL ({exc.__class__.__name__}).\n"
            "Start the stack first, then re-run:\n\n"
            "    docker compose up --build\n"
            "    python -m scripts.seed_demo\n"
        )
        return False


def _make_repos():
    from app.core.database import is_postgres_enabled

    if not is_postgres_enabled():
        print(
            "seed_demo requires PostgreSQL so the demo data persists.\n"
            "Start the stack first, then re-run:\n\n"
            "    docker compose up --build\n"
            "    python -m scripts.seed_demo\n"
        )
        sys.exit(1)

    from app.repositories.postgres_lead_repo import PostgresLeadRepository
    from app.repositories.postgres_widget_repo import PostgresWidgetRepository

    return PostgresWidgetRepository(), PostgresLeadRepository()


async def _seed_widgets(widget_repo, tenant_id):
    existing, _ = await widget_repo.list_by_tenant(tenant_id)
    by_name = {w.name: w for w in existing}

    seeded = []
    for spec in WIDGET_SPECS:
        widget = by_name.get(spec["name"])
        if widget is not None:
            print(f"  widget '{spec['name']}' already seeded — skipping")
        else:
            widget = await widget_repo.create(
                name=spec["name"],
                domain=spec["domain"],
                config=spec["config"],
                tenant_id=tenant_id,
            )
            print(f"  widget '{spec['name']}' created: {widget.id}")
        seeded.append((spec, widget))
    return seeded


async def _seed_leads(lead_repo, tenant_id, seeded):
    existing, _ = await lead_repo.list_by_tenant(tenant_id)
    if existing:
        print(f"  {len(existing)} leads already seeded — skipping")
        return

    count = 0
    for spec, widget in seeded:
        for entry in spec["leads"]:
            form_data = dict(entry["form_data"])
            fingerprint = compute_fingerprint(
                str(widget.id), entry["ip_address"], form_data
            )
            lead = await lead_repo.create(
                widget_id=str(widget.id),
                tenant_id=tenant_id,
                form_data=form_data,
                ip_address=entry["ip_address"],
                fingerprint=fingerprint,
                user_agent=DEFAULT_USER_AGENT,
                referer=spec["domain"] + "/",
                spam_score=entry.get("spam_score", 0.0),
                spam_reasons=entry.get("spam_reasons"),
                honeypot_triggered=entry.get("honeypot_triggered", False),
                status="pending",
            )
            geo = entry.get("geo")
            if geo:
                await lead_repo.update_status(str(lead.id), "enriched", **geo)
            count += 1
    print(f"  {count} leads seeded")


def _print_summary(tenant_id, seeded):
    print()
    print("=" * 72)
    print("Demo tenant id:", tenant_id)
    print()
    for spec, widget in seeded:
        print(
            f"widget '{spec['name']}'  id={widget.id}  js_version={widget.js_version}"
        )
        print("  embed snippet:")
        print(
            f'    <script src="http://localhost:8000/public/widget/{widget.id}/widget.js?v={widget.js_version}" data-widget-id="{widget.id}" defer></script>'
        )
        print(
            f"  config endpoint:   http://localhost:8000/public/widget/{widget.id}/config"
        )
        print(
            f"  submit endpoint:   http://localhost:8000/public/widget/{widget.id}/submit"
        )
        print()
    print("Dashboard (authenticated, Bearer token required):")
    print(
        "  GET /widgets/{id}/leads   GET /widgets/{id}/stats   GET /widgets/{id}/export"
    )
    print("  GET /leads                GET /leads/stats")
    print("=" * 72)


async def main():
    print("Seeding demo data (requires PostgreSQL from `docker compose up --build`)...")
    widget_repo, lead_repo = _make_repos()
    if not await _check_connection():
        sys.exit(1)
    seeded = await _seed_widgets(widget_repo, DEMO_TENANT_ID)
    await _seed_leads(lead_repo, DEMO_TENANT_ID, seeded)
    _print_summary(DEMO_TENANT_ID, seeded)


if __name__ == "__main__":
    asyncio.run(main())
