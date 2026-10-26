import uuid

import pytest
from fastapi.testclient import TestClient

from app.dependencies.auth import get_current_user
from app.main import app

pytestmark = pytest.mark.usefixtures("mock_redis")


@pytest.fixture
def client():
    return TestClient(app)


MOCK_USER = {
    "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
    "email": "test@example.com",
    "access_token": "fake-token",
}


@pytest.fixture(autouse=True)
def _mock_auth():
    app.dependency_overrides[get_current_user] = lambda: MOCK_USER
    yield
    app.dependency_overrides.pop(get_current_user, None)


class TestSubmitLead:
    def test_201_success(self, client, created_widget):
        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={
                "form_data": {"name": "John", "email": "john@test.com"},
            },
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is True
        assert "lead_id" in data

    def test_trusted_proxy_xff_stores_forwarded_ip(
        self, client, created_widget, monkeypatch
    ):
        from app.services import lead_service

        monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "172.18.0.0/16")
        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": {"name": "John", "email": "john@test.com"}},
            headers={
                "Origin": "https://myshop.com",
                "X-Forwarded-For": "8.8.8.8",
            },
        )
        assert resp.status_code == 201
        lead_id = resp.json()["lead_id"]
        repo = lead_service._get_or_create_repo()
        assert repo._leads[lead_id]["ip_address"] == "8.8.8.8"

    def test_422_validation_error(self, client, created_widget):
        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": "not-an-object"},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 422

    def test_403_origin_mismatch(self, client, created_widget):
        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": {"name": "John"}},
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403

    def test_404_widget_not_found(self, client):
        resp = client.post(
            "/public/widget/00000000-0000-0000-0000-000000000000/submit",
            json={"form_data": {"name": "John"}},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 404

    def test_413_payload_too_large(self, client, created_widget):
        widget_id = str(created_widget.id)
        big_data = {"form_data": {"x": "a" * 60_000}}
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json=big_data,
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 413

    def test_429_rate_limited(self, client, created_widget, fake_redis, monkeypatch):
        async def mock_over_limit(ip, widget_id):
            return 60

        monkeypatch.setattr(
            "app.services.lead_service.check_rate_limits", mock_over_limit
        )

        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": {"name": "John"}},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 429

    def test_429_retry_after_header(self, client, created_widget, monkeypatch):
        async def mock_over_limit(ip, widget_id):
            return 60

        monkeypatch.setattr(
            "app.services.lead_service.check_rate_limits", mock_over_limit
        )

        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": {"name": "John"}},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 429
        assert resp.headers.get("retry-after") == "60"

    def test_window_reset_after_retry_after_expires(
        self, client, created_widget, monkeypatch
    ):
        widget_id = str(created_widget.id)
        payload = {"form_data": {"name": "John"}}
        headers = {"Origin": "https://myshop.com"}

        call_count = [0]

        async def mock_check_rates(ip, widget_id):
            call_count[0] += 1
            if call_count[0] <= 1:
                return 60
            return None

        monkeypatch.setattr(
            "app.services.lead_service.check_rate_limits", mock_check_rates
        )

        resp1 = client.post(
            f"/public/widget/{widget_id}/submit", json=payload, headers=headers
        )
        assert resp1.status_code == 429
        assert resp1.headers.get("retry-after") == "60"

        resp2 = client.post(
            f"/public/widget/{widget_id}/submit", json=payload, headers=headers
        )
        assert resp2.status_code == 201

    def test_rate_limit_takes_precedence_over_dedup(
        self, client, created_widget, monkeypatch
    ):
        widget_id = str(created_widget.id)
        payload = {"form_data": {"name": "John"}}
        headers = {"Origin": "https://myshop.com"}

        resp1 = client.post(
            f"/public/widget/{widget_id}/submit", json=payload, headers=headers
        )
        assert resp1.status_code == 201

        async def mock_over_limit(ip, widget_id):
            return 60

        monkeypatch.setattr(
            "app.services.lead_service.check_rate_limits", mock_over_limit
        )

        resp2 = client.post(
            f"/public/widget/{widget_id}/submit", json=payload, headers=headers
        )
        assert resp2.status_code == 429
        assert resp2.headers.get("retry-after") == "60"

    def test_honeypot_returns_fake_success(self, client, created_widget):
        import asyncio

        from app.services import widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        honeypot_field = raw["config"].get("honeypot_field", "_hp_a3f9")

        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={
                "form_data": {
                    "name": "Bot",
                    "email": "bot@spam.com",
                    honeypot_field: "bot value",
                },
            },
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is True

    def test_dedup_returns_same_lead_id(self, client, created_widget):
        widget_id = str(created_widget.id)
        payload = {
            "form_data": {"name": "John", "email": "john@test.com"},
        }
        headers = {"Origin": "https://myshop.com"}

        resp1 = client.post(
            f"/public/widget/{widget_id}/submit", json=payload, headers=headers
        )
        assert resp1.status_code == 201
        lead_id1 = resp1.json()["lead_id"]

        resp2 = client.post(
            f"/public/widget/{widget_id}/submit", json=payload, headers=headers
        )
        assert resp2.status_code == 201
        lead_id2 = resp2.json()["lead_id"]

        assert lead_id1 == lead_id2


class TestValidationEdgeCases:
    def test_malformed_json_body(self, client, created_widget):
        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            content=b"{bad json]}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://myshop.com",
            },
        )
        assert resp.status_code == 422

    def test_empty_body_rejected(self, client, created_widget):
        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://myshop.com",
            },
        )
        assert resp.status_code == 422

    def test_invalid_widget_id_format(self, client):
        resp = client.post(
            "/public/widget/abc/submit",
            json={"form_data": {"name": "John"}},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 422

    def test_path_traversal_widget_id(self, client):
        resp = client.post(
            "/public/widget/../../etc/passwd/submit",
            json={"form_data": {"name": "John"}},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 404

    def test_missing_content_type(self, client, created_widget):
        import json as _json

        widget_id = str(created_widget.id)
        body = _json.dumps({"form_data": {"name": "John"}})
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            content=body,
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["success"] is True
        assert "lead_id" in data

    def test_xml_content_type_rejected(self, client, created_widget):
        import json as _json

        widget_id = str(created_widget.id)
        body = _json.dumps({"form_data": {"name": "John"}})
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            content=body,
            headers={
                "Content-Type": "application/xml",
                "Origin": "https://myshop.com",
            },
        )
        assert resp.status_code == 422

    def test_empty_form_data_dict_rejected(self, client, created_widget):
        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": {}},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 422


class TestReEnrichLead:
    def test_202_re_enqueues_failed_lead(self, client, created_widget):
        from app.services import lead_service

        repo = lead_service._get_or_create_repo()
        lead_id = str(uuid.uuid4())
        repo._leads[lead_id] = {
            "id": lead_id,
            "widget_id": str(created_widget.id),
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "form_data": {"name": "John"},
            "ip_address": "8.8.8.8",
            "fingerprint": "abc",
            "status": "failed",
            "honeypot_triggered": False,
            "spam_score": 0.0,
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
        }

        from tests.conftest import _fake_queue

        _fake_queue.enqueued_jobs.clear()

        resp = client.post(
            f"/widgets/{created_widget.id}/leads/{lead_id}/re-enrich",
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "re-enqueued"
        assert data["lead_id"] == lead_id

    def test_409_if_already_enriched(self, client, created_widget):
        from app.services import lead_service

        repo = lead_service._get_or_create_repo()
        lead_id = str(uuid.uuid4())
        repo._leads[lead_id] = {
            "id": lead_id,
            "widget_id": str(created_widget.id),
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "form_data": {"name": "John"},
            "ip_address": "8.8.8.8",
            "fingerprint": "abc",
            "status": "enriched",
            "honeypot_triggered": False,
            "spam_score": 0.0,
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
        }

        resp = client.post(
            f"/widgets/{created_widget.id}/leads/{lead_id}/re-enrich",
        )
        assert resp.status_code == 409

    def test_409_if_in_flight_pending(self, client, created_widget):
        from app.services import lead_service

        repo = lead_service._get_or_create_repo()
        lead_id = str(uuid.uuid4())
        repo._leads[lead_id] = {
            "id": lead_id,
            "widget_id": str(created_widget.id),
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "form_data": {"name": "John"},
            "ip_address": "8.8.8.8",
            "fingerprint": "abc",
            "status": "pending",
            "honeypot_triggered": False,
            "spam_score": 0.0,
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
        }

        resp = client.post(
            f"/widgets/{created_widget.id}/leads/{lead_id}/re-enrich",
        )
        assert resp.status_code == 409

    def test_404_lead_not_found(self, client, created_widget):
        resp = client.post(
            f"/widgets/{created_widget.id}/leads/"
            f"00000000-0000-0000-0000-000000000000/re-enrich",
        )
        assert resp.status_code == 404

    def test_404_widget_not_found(self, client):
        resp = client.post(
            "/widgets/00000000-0000-0000-0000-000000000000/leads/"
            "00000000-0000-0000-0000-000000000000/re-enrich",
        )
        assert resp.status_code == 404

    def test_re_enrich_invalid_lead_id(self, client, created_widget):
        resp = client.post(
            f"/widgets/{created_widget.id}/leads/abc/re-enrich",
        )
        assert resp.status_code == 422


class TestListWidgetLeads:
    def test_200_empty_list(self, client, created_widget):
        resp = client.get(f"/widgets/{created_widget.id}/leads")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert data["pages"] == 0
        assert data["items"] == []

    def test_200_honeypot_excluded_by_default(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])
        hp_field = raw["config"]["honeypot_field"]

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Clean"},
                ip_address="1.1.1.1",
                fingerprint="fp-clean",
            )
        )
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Bot", hp_field: "x"},
                ip_address="2.2.2.2",
                fingerprint="fp-bot",
                honeypot_triggered=True,
                spam_score=1.0,
                spam_reasons=["honeypot"],
            )
        )

        resp = client.get(f"/widgets/{widget_id}/leads")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    def test_200_include_honeypot_shows_all(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Clean"},
                ip_address="1.1.1.1",
                fingerprint="fp-clean",
            )
        )
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Bot"},
                ip_address="2.2.2.2",
                fingerprint="fp-bot",
                honeypot_triggered=True,
            )
        )

        resp = client.get(f"/widgets/{widget_id}/leads?include_honeypot=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    def test_404_widget_not_found(self, client):
        resp = client.get("/widgets/00000000-0000-0000-0000-000000000000/leads")
        assert resp.status_code == 404

    def test_200_pagination_params(self, client, created_widget):
        resp = client.get(f"/widgets/{created_widget.id}/leads?page=1&page_size=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["page_size"] == 10

    def test_200_search_filter(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Alice"},
                ip_address="1.1.1.1",
                fingerprint="fp-alice",
            )
        )
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Bob"},
                ip_address="2.2.2.2",
                fingerprint="fp-bob",
            )
        )

        resp = client.get(f"/widgets/{widget_id}/leads?search=Alice")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    def test_200_status_filter(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Pending"},
                ip_address="1.1.1.1",
                fingerprint="fp-p",
                status="pending",
            )
        )
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Enriched"},
                ip_address="2.2.2.2",
                fingerprint="fp-e",
                status="enriched",
            )
        )

        resp = client.get(f"/widgets/{widget_id}/leads?status=enriched")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1


class TestCrossWidgetLeads:
    def test_200_list_all_leads(self, client, created_widget):
        resp = client.get("/leads")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["page"] == 1

    def test_200_list_all_leads_pagination(self, client, created_widget):
        resp = client.get("/leads?page=1&page_size=5")
        assert resp.status_code == 200


class TestLeadDetail:
    def test_200_lead_detail(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        lead = asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Detail Test"},
                ip_address="1.1.1.1",
                fingerprint="fp-detail",
            )
        )

        resp = client.get(f"/widgets/{widget_id}/leads/{lead.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fingerprint"] == "fp-detail"
        assert data["form_data"]["name"] == "Detail Test"

    def test_404_lead_not_found(self, client, created_widget):
        resp = client.get(
            f"/widgets/{created_widget.id}/leads/"
            f"00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404

    def test_404_widget_not_found(self, client):
        resp = client.get(
            "/widgets/00000000-0000-0000-0000-000000000000/leads/"
            "00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404

    def test_lead_detail_invalid_lead_id(self, client, created_widget):
        resp = client.get(
            f"/widgets/{created_widget.id}/leads/abc",
        )
        assert resp.status_code == 422


class TestWidgetStats:
    def test_200_stats(self, client, created_widget):
        resp = client.get(f"/widgets/{created_widget.id}/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_leads" in data
        assert "today" in data
        assert "this_week" in data
        assert "this_month" in data
        assert "avg_spam_score" in data
        assert "honeypot_blocked" in data
        assert "top_countries" in data
        assert "leads_over_time" in data

    def test_200_stats_honeypot_excluded_from_counts(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Clean"},
                ip_address="1.1.1.1",
                fingerprint="fp-clean",
            )
        )
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Bot"},
                ip_address="2.2.2.2",
                fingerprint="fp-bot",
                honeypot_triggered=True,
            )
        )

        resp = client.get(f"/widgets/{widget_id}/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_leads"] == 1
        assert data["honeypot_blocked"] == 1

    def test_404_widget_not_found(self, client):
        resp = client.get("/widgets/00000000-0000-0000-0000-000000000000/stats")
        assert resp.status_code == 404


class TestGlobalStats:
    def test_200_global_stats(self, client):
        resp = client.get("/leads/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_leads" in data

    def test_global_stats_excludes_honeypot(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Clean"},
                ip_address="1.1.1.1",
                fingerprint="fp-clean",
            )
        )
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Bot"},
                ip_address="2.2.2.2",
                fingerprint="fp-bot",
                honeypot_triggered=True,
            )
        )

        resp = client.get("/leads/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_leads"] == 1
        assert data["honeypot_blocked"] == 1


class TestLeadFilters:
    def test_sort_order_query_param(self, client, created_widget):
        import asyncio
        from datetime import datetime, timedelta, timezone

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        now = datetime.now(timezone.utc)

        lead1 = asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Alpha"},
                ip_address="1.1.1.1",
                fingerprint="fp-1",
            )
        )
        repo._leads[str(lead1.id)]["created_at"] = now - timedelta(hours=2)

        lead2 = asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Beta"},
                ip_address="2.2.2.2",
                fingerprint="fp-2",
            )
        )
        repo._leads[str(lead2.id)]["created_at"] = now

        resp = client.get(f"/widgets/{widget_id}/leads")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["form_data"]["name"] == "Beta"

        resp = client.get(f"/widgets/{widget_id}/leads?sort_order=asc")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"][0]["form_data"]["name"] == "Alpha"

    def test_date_from_date_to_query_params(self, client, created_widget):
        import asyncio
        from datetime import datetime, timedelta, timezone

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        now = datetime.now(timezone.utc)

        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Old"},
                ip_address="1.1.1.1",
                fingerprint="fp-old",
            )
        )

        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Current"},
                ip_address="2.2.2.2",
                fingerprint="fp-cur",
            )
        )

        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        resp = client.get(
            f"/widgets/{widget_id}/leads" f"?date_from={yesterday}&date_to={tomorrow}"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

        resp = client.get(
            f"/widgets/{widget_id}/leads" f"?date_from=2020-01-01&date_to=2020-12-31"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0


class TestSubmissionPersistence:
    def test_created_at_updated_at_timestamps(self, client, created_widget):
        import asyncio
        from datetime import datetime

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": {"name": "John", "email": "john@test.com"}},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 201
        lead_id = resp.json()["lead_id"]

        resp = client.get(f"/widgets/{widget_id}/leads/{lead_id}")
        assert resp.status_code == 200
        data = resp.json()

        created_at = data["created_at"]
        updated_at = data["updated_at"]

        dt_created = datetime.fromisoformat(created_at)
        dt_updated = datetime.fromisoformat(updated_at)
        assert dt_created.tzinfo is not None
        assert dt_updated.tzinfo is not None

        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        str(raw["tenant_id"])
        repo = lead_service._get_or_create_repo()
        asyncio.run(repo.update_status(lead_id, "enriched"))

        resp = client.get(f"/widgets/{widget_id}/leads/{lead_id}")
        data = resp.json()
        assert data["created_at"] == created_at
        updated_at2 = datetime.fromisoformat(data["updated_at"])
        assert updated_at2 > dt_updated

    def test_fingerprint_persisted_on_lead(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service
        from app.services.fingerprint_service import compute_fingerprint

        widget_id = str(created_widget.id)
        form_data = {"name": "John", "email": "john@test.com"}
        resp = client.post(
            f"/public/widget/{widget_id}/submit",
            json={"form_data": form_data},
            headers={"Origin": "https://myshop.com"},
        )
        assert resp.status_code == 201
        lead_id = resp.json()["lead_id"]

        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        str(raw["tenant_id"])
        repo = lead_service._get_or_create_repo()
        lead = asyncio.run(repo.get_by_id(lead_id))
        assert lead is not None

        expected_fp = compute_fingerprint(widget_id, lead.ip_address, form_data)
        assert lead.fingerprint == expected_fp


class TestExportCSV:
    def test_200_csv_headers(self, client, created_widget):
        resp = client.get(f"/widgets/{created_widget.id}/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "Content-Disposition" in resp.headers
        assert resp.headers["content-disposition"].startswith("attachment")
        assert str(created_widget.id) in resp.headers["content-disposition"]

    def test_200_csv_content(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "CSV User"},
                ip_address="1.1.1.1",
                fingerprint="fp-csv",
            )
        )

        resp = client.get(f"/widgets/{widget_id}/export")
        assert resp.status_code == 200
        assert "CSV User" in resp.text
        assert resp.text.startswith("id,")

    def test_404_widget_not_found(self, client):
        resp = client.get("/widgets/00000000-0000-0000-0000-000000000000/export")
        assert resp.status_code == 404

    def test_export_truncated_header(self, client, created_widget, monkeypatch):
        import asyncio

        from app.services import lead_service, widget_service

        monkeypatch.setattr("app.services.lead_service.MAX_EXPORT_ROWS", 1)

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "User 1"},
                ip_address="1.1.1.1",
                fingerprint="fp-1",
            )
        )
        asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "User 2"},
                ip_address="1.1.1.2",
                fingerprint="fp-2",
            )
        )

        resp = client.get(f"/widgets/{widget_id}/export")
        assert resp.status_code == 200
        assert resp.headers.get("X-Export-Truncated") == "true"


class TestDeleteLead:
    def test_204_delete(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        lead = asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id=tenant_id,
                form_data={"name": "Delete Me"},
                ip_address="1.1.1.1",
                fingerprint="fp-del",
            )
        )

        resp = client.delete(f"/widgets/{widget_id}/leads/{lead.id}")
        assert resp.status_code == 204

    def test_404_delete_not_found(self, client, created_widget):
        resp = client.delete(
            f"/widgets/{created_widget.id}/leads/"
            f"00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404


class TestBatchDelete:
    def test_204_batch_delete(self, client, created_widget):
        import asyncio

        from app.services import lead_service, widget_service

        widget_id = str(created_widget.id)
        raw = asyncio.run(widget_service._get_repo().get_by_id_raw(widget_id))
        tenant_id = str(raw["tenant_id"])

        repo = lead_service._get_or_create_repo()
        ids = []
        for i in range(3):
            lead = asyncio.run(
                repo.create(
                    widget_id=widget_id,
                    tenant_id=tenant_id,
                    form_data={"name": f"User {i}"},
                    ip_address="1.1.1.1",
                    fingerprint=f"fp-batch-{i}",
                )
            )
            ids.append(str(lead.id))

        resp = client.post(
            f"/widgets/{widget_id}/leads/batch-delete",
            json={"lead_ids": ids},
        )
        assert resp.status_code == 204


class TestAuthEdgeCases:
    def test_401_without_auth(self, client, created_widget):
        app.dependency_overrides.pop(get_current_user, None)
        resp = client.get(f"/widgets/{created_widget.id}/leads")
        assert resp.status_code == 401

    def test_404_wrong_tenant_on_lead_detail(self, client, created_widget):
        import asyncio

        from app.services import lead_service

        other_tenant = "22222222-2222-2222-2222-222222222222"
        other_user = {
            "id": uuid.UUID(other_tenant),
            "email": "other@example.com",
            "access_token": "other-token",
        }
        app.dependency_overrides[get_current_user] = lambda: other_user

        widget_id = str(created_widget.id)
        repo = lead_service._get_or_create_repo()
        lead = asyncio.run(
            repo.create(
                widget_id=widget_id,
                tenant_id="11111111-1111-1111-1111-111111111111",
                form_data={"name": "Other Tenant"},
                ip_address="1.1.1.1",
                fingerprint="fp-other",
            )
        )

        resp = client.get(f"/widgets/{widget_id}/leads/{lead.id}")
        assert resp.status_code == 404
