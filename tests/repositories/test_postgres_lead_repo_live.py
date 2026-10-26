import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.core import database
from app.repositories.postgres_lead_repo import PostgresLeadRepository

# Conftest sets DATABASE_URL="" for the offline suite; this live suite reassigns the
# module global so get_pool() connects to the real compose `db` service.
TEST_DATABASE_URL = os.environ.get(
    "FLYRANK_TEST_DATABASE_URL",
    "postgresql://flyrank:flyrank_pass@127.0.0.1:5432/flyrank",
)
database.DATABASE_URL = TEST_DATABASE_URL

TENANT_ID = "99999999-9999-4999-8999-999999999999"
WIDGET_ID = "88888888-8888-4888-8888-888888888888"
WIDGET_DOMAIN = "https://live-test.example.com"


@pytest.fixture
async def live_db():
    pool = await database.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM widgets WHERE id = $1", UUID(WIDGET_ID))
        await conn.execute(
            """
            INSERT INTO widgets (id, tenant_id, name, domain, config)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            UUID(WIDGET_ID),
            UUID(TENANT_ID),
            "live-test-widget",
            WIDGET_DOMAIN,
            '{"brand_color": "#2563eb"}',
        )
    yield PostgresLeadRepository()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM widgets WHERE id = $1", UUID(WIDGET_ID))
    await database.close_pool()


def _form(**overrides):
    data = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+15550001111",
        "message": "Hello from the live test",
    }
    data.update(overrides)
    return data


class TestPostgresLeadRepositoryLive:
    async def test_create_returns_lead_with_all_expected_fields(self, live_db):
        repo = live_db
        lead = await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(),
            ip_address="203.0.113.5",
            fingerprint="fp-create-1",
            user_agent="live-test-agent",
            referer="https://live-test.example.com/contact",
            spam_score=0.3,
            spam_reasons=["urls"],
            honeypot_triggered=False,
        )
        assert lead.id is not None
        assert str(lead.widget_id) == WIDGET_ID
        assert str(lead.tenant_id) == TENANT_ID
        assert lead.form_data["name"] == "Ada Lovelace"
        assert lead.ip_address == "203.0.113.5"
        assert isinstance(lead.ip_address, str)
        assert lead.user_agent == "live-test-agent"
        assert lead.referer == "https://live-test.example.com/contact"
        assert lead.fingerprint == "fp-create-1"
        assert lead.spam_score == pytest.approx(0.3)
        assert lead.spam_reasons == ["urls"]
        assert lead.honeypot_triggered is False
        assert lead.status == "pending"
        assert lead.created_at.tzinfo is not None

    async def test_create_ipv6_address_round_trips_as_string(self, live_db):
        repo = live_db
        lead = await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="IPv6 User"),
            ip_address="2001:db8::1",
            fingerprint="fp-ipv6-1",
        )
        fetched = await repo.get_by_id(str(lead.id))
        assert fetched is not None
        assert fetched.ip_address == "2001:db8::1"
        assert isinstance(fetched.ip_address, str)

    async def test_create_invalid_ip_uses_sentinel(self, live_db):
        repo = live_db
        lead = await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Sentinel User"),
            ip_address="unknown",
            fingerprint="fp-sentinel-1",
        )
        assert lead.ip_address == "0.0.0.0"

    async def test_get_by_id_returns_none_for_missing(self, live_db):
        repo = live_db
        assert await repo.get_by_id("00000000-0000-4000-8000-000000000000") is None

    async def test_list_by_widget_returns_items_and_total(self, live_db):
        repo = live_db
        await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="A"),
            ip_address="203.0.113.10",
            fingerprint="fp-list-1",
        )
        await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="B"),
            ip_address="203.0.113.11",
            fingerprint="fp-list-2",
        )
        items, total = await repo.list_by_widget(WIDGET_ID, TENANT_ID)
        assert total == 2
        assert len(items) == 2
        assert {i.form_data["name"] for i in items} == {"A", "B"}

    async def test_list_by_widget_applies_filters_and_pagination(self, live_db):
        repo = live_db
        base = datetime.now(timezone.utc).date()
        for i, score in enumerate((0.1, 0.5, 0.9)):
            await repo.create(
                widget_id=WIDGET_ID,
                tenant_id=TENANT_ID,
                form_data=_form(name=f"Filter {i}", email=f"f{i}@example.com"),
                ip_address=f"203.0.113.{20 + i}",
                fingerprint=f"fp-filters-{i}",
                spam_score=score,
                status="enriched" if i % 2 == 0 else "pending",
            )
        page1, total = await repo.list_by_widget(
            WIDGET_ID,
            TENANT_ID,
            search="Filter",
            status="enriched",
            spam_min=0.0,
            spam_max=1.0,
            date_from=base - timedelta(days=1),
            date_to=base + timedelta(days=1),
            sort_by="spam_score",
            sort_order="asc",
            page=1,
            page_size=1,
        )
        assert total == 2
        assert len(page1) == 1
        assert page1[0].status == "enriched"
        page2, _ = await repo.list_by_widget(
            WIDGET_ID,
            TENANT_ID,
            search="Filter",
            status="enriched",
            sort_by="spam_score",
            sort_order="asc",
            page=2,
            page_size=1,
        )
        assert len(page2) == 1

    async def test_list_by_widget_excludes_honeypot_by_default(self, live_db):
        repo = live_db
        await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Bot"),
            ip_address="203.0.113.30",
            fingerprint="fp-hp-1",
            honeypot_triggered=True,
            spam_score=1.0,
        )
        items, _ = await repo.list_by_widget(WIDGET_ID, TENANT_ID)
        assert all(not i.honeypot_triggered for i in items)

    async def test_list_by_widget_includes_honeypot_when_requested(self, live_db):
        repo = live_db
        await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Bot2"),
            ip_address="203.0.113.31",
            fingerprint="fp-hp-2",
            honeypot_triggered=True,
        )
        items, _ = await repo.list_by_widget(
            WIDGET_ID, TENANT_ID, include_honeypot=True
        )
        assert any(i.honeypot_triggered for i in items)

    async def test_list_by_tenant_returns_items_and_total(self, live_db):
        repo = live_db
        await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Tenant A"),
            ip_address="203.0.113.40",
            fingerprint="fp-tenant-1",
        )
        items, total = await repo.list_by_tenant(TENANT_ID)
        assert total >= 1
        assert any(i.form_data["name"] == "Tenant A" for i in items)

    async def test_update_status_updates_status_and_returns_updated(self, live_db):
        repo = live_db
        lead = await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Status"),
            ip_address="203.0.113.50",
            fingerprint="fp-status-1",
        )
        updated = await repo.update_status(str(lead.id), "enriched")
        assert updated is not None
        assert updated.status == "enriched"
        fetched = await repo.get_by_id(str(lead.id))
        assert fetched.status == "enriched"

    async def test_update_status_with_geo_extras(self, live_db):
        repo = live_db
        lead = await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Geo"),
            ip_address="8.8.8.8",
            fingerprint="fp-geo-1",
        )
        updated = await repo.update_status(
            str(lead.id),
            "enriched",
            geo_country="United States",
            geo_city="Mountain View",
            geo_provider="ipinfo",
        )
        assert updated.geo_country == "United States"
        assert updated.geo_city == "Mountain View"
        assert updated.geo_provider == "ipinfo"

    async def test_update_status_returns_none_for_missing(self, live_db):
        repo = live_db
        assert (
            await repo.update_status("00000000-0000-4000-8000-000000000000", "failed")
            is None
        )

    async def test_delete_returns_true_and_removes(self, live_db):
        repo = live_db
        lead = await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Del"),
            ip_address="203.0.113.60",
            fingerprint="fp-del-1",
        )
        deleted = await repo.delete(str(lead.id), WIDGET_ID, TENANT_ID)
        assert deleted is True
        assert await repo.get_by_id(str(lead.id)) is None

    async def test_delete_returns_false_for_missing(self, live_db):
        repo = live_db
        deleted = await repo.delete(
            "00000000-0000-4000-8000-000000000000", WIDGET_ID, TENANT_ID
        )
        assert deleted is False

    async def test_batch_delete_removes_selected(self, live_db):
        repo = live_db
        leads = [
            await repo.create(
                widget_id=WIDGET_ID,
                tenant_id=TENANT_ID,
                form_data=_form(name=f"Batch {i}"),
                ip_address=f"203.0.113.{70 + i}",
                fingerprint=f"fp-batch-{i}",
            )
            for i in range(3)
        ]
        count = await repo.batch_delete(
            [str(l.id) for l in leads[:2]], WIDGET_ID, TENANT_ID
        )
        assert count == 2
        items, total = await repo.list_by_widget(
            WIDGET_ID, TENANT_ID, include_honeypot=True
        )
        assert total >= 1
        remaining_ids = {str(i.id) for i in items}
        assert str(leads[0].id) not in remaining_ids
        assert str(leads[1].id) not in remaining_ids

    async def test_batch_delete_empty_returns_zero(self, live_db):
        repo = live_db
        assert await repo.batch_delete([], WIDGET_ID, TENANT_ID) == 0

    async def test_get_stats_returns_expected_shape(self, live_db):
        repo = live_db
        lead = await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Stats A"),
            ip_address="8.8.4.4",
            fingerprint="fp-stats-1",
            spam_score=0.25,
        )
        await repo.update_status(
            str(lead.id),
            "enriched",
            geo_country="United States",
            geo_provider="ipapi",
        )
        await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Stats Bot"),
            ip_address="203.0.113.80",
            fingerprint="fp-stats-2",
            spam_score=1.0,
            honeypot_triggered=True,
        )
        stats = await repo.get_stats(WIDGET_ID, TENANT_ID)
        assert stats["total_leads"] >= 1
        assert stats["today"] >= 1
        assert stats["this_week"] >= 1
        assert stats["this_month"] >= 1
        assert stats["avg_spam_score"] >= 0.0
        assert stats["honeypot_blocked"] >= 1
        assert any(c["country"] == "United States" for c in stats["top_countries"])
        assert len(stats["leads_over_time"]) >= 1

    async def test_get_tenant_stats_returns_expected_shape(self, live_db):
        repo = live_db
        stats = await repo.get_tenant_stats(TENANT_ID)
        assert "total_leads" in stats
        assert "today" in stats
        assert "avg_spam_score" in stats
        assert "honeypot_blocked" in stats

    async def test_get_export_data_returns_model_dumps(self, live_db):
        repo = live_db
        await repo.create(
            widget_id=WIDGET_ID,
            tenant_id=TENANT_ID,
            form_data=_form(name="Export 1"),
            ip_address="203.0.113.90",
            fingerprint="fp-export-1",
        )
        rows = await repo.get_export_data(WIDGET_ID, TENANT_ID)
        assert isinstance(rows, list)
        assert len(rows) >= 1
        row = rows[0]
        assert isinstance(row["id"], UUID)
        assert isinstance(row["ip_address"], str)
        assert row["form_data"]["name"] == "Export 1"

    def test_f9_second_asyncio_run_reuses_closed_loop_pool(self):
        """F9 regression (live Postgres).

        RQ calls the enrichment job function once per job, and that function
        wraps every DB op in its own asyncio.run() (see
        app/services/lead_worker.py run_enrichment_job). The module-level
        asyncpg pool is therefore created on the first asyncio.run()'s loop and
        left bound to it after that loop closes. A second asyncio.run() for the
        same job must still work: get_pool() must detect the loop change and
        recreate the pool. Pre-fix the second run reuses the stale pool and
        raises InterfaceError / 'Event loop is closed'; post-fix it succeeds.

        Mirrors the M29 repro exactly: run #1 (create) succeeds, run #2
        (get_by_id + update_status) is where the bug surfaced.
        """
        from app.core import database as db

        def _run1() -> str:
            db._pool = None
            db._pool_loop = None

            async def _body() -> str:
                pool = await db.get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM widgets WHERE id = $1", UUID(WIDGET_ID)
                    )
                    await conn.execute(
                        """
                        INSERT INTO widgets (id, tenant_id, name, domain, config)
                        VALUES ($1, $2, $3, $4, $5::jsonb)
                        """,
                        UUID(WIDGET_ID),
                        UUID(TENANT_ID),
                        "f9-live-test-widget",
                        WIDGET_DOMAIN,
                        '{"brand_color": "#2563eb"}',
                    )
                repo = PostgresLeadRepository()
                lead = await repo.create(
                    widget_id=WIDGET_ID,
                    tenant_id=TENANT_ID,
                    form_data=_form(name="F9 Second Run"),
                    ip_address="203.0.113.200",
                    fingerprint="fp-f9-live",
                )
                return str(lead.id)

            return asyncio.run(_body())

        def _run2(lead_id: str) -> None:
            repo = PostgresLeadRepository()
            lead = asyncio.run(repo.get_by_id(lead_id))
            assert lead is not None
            assert lead.status == "pending"
            updated = asyncio.run(
                repo.update_status(lead_id, "enriched", geo_country="United States")
            )
            assert updated is not None
            assert updated.status == "enriched"
            assert updated.geo_country == "United States"

        def _cleanup(lead_id: str) -> None:
            async def _inner() -> None:
                pool = await db.get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM leads WHERE id = $1", lead_id)
                    await conn.execute(
                        "DELETE FROM widgets WHERE id = $1", UUID(WIDGET_ID)
                    )
                await db.close_pool()

            asyncio.run(_inner())

        lead_id = _run1()
        try:
            _run2(lead_id)
        finally:
            _cleanup(lead_id)
