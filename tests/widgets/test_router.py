import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.dependencies.auth import get_current_user
from app.main import app
from app.models.widget import WidgetResponse


def _make_response(
    id: str | None = None,
    name: str = "Test Widget",
    domain: str = "https://example.com",
    config: dict | None = None,
    js_version: int = 1,
    active: bool = True,
    tenant_id: str | None = None,
):
    now = datetime.now(timezone.utc)
    return WidgetResponse(
        id=id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        name=name,
        domain=domain,
        config=config
        or {
            "brand_color": "#2563eb",
            "button_text": "Get a Quote",
            "fields": ["name", "email"],
            "success_message": "Thanks!",
            "honeypot_field": "_hp_test",
        },
        js_version=js_version,
        active=active,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _mock_auth_and_service(monkeypatch):
    mock_user = {
        "id": uuid.uuid4(),
        "email": "test@example.com",
        "access_token": "fake-token",
    }

    app.dependency_overrides[get_current_user] = lambda: mock_user

    mock_service = AsyncMock()
    monkeypatch.setattr("app.routers.widgets.widget_service", mock_service)
    yield mock_service, mock_user
    app.dependency_overrides.pop(get_current_user, None)


class TestListWidgets:
    def test_list_widgets_returns_200(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        widget = _make_response(tenant_id=str(mock_user["id"]))
        mock_service.get_widgets.return_value = ([widget], 1)

        response = client.get("/widgets/")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["page"] == 1
        assert data["page_size"] == 20

    def test_list_widgets_with_search(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        widget = _make_response(name="Alpha", tenant_id=str(mock_user["id"]))
        mock_service.get_widgets.return_value = ([widget], 1)

        response = client.get("/widgets/?search=alpha")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Alpha"

    def test_list_widgets_empty(self, client, _mock_auth_and_service):
        mock_service, _ = _mock_auth_and_service
        mock_service.get_widgets.return_value = ([], 0)

        response = client.get("/widgets/")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_widgets_pages_field(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        widget = _make_response(tenant_id=str(mock_user["id"]))
        mock_service.get_widgets.return_value = ([widget], 1)

        resp = client.get("/widgets/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pages"] == 1

        widgets = [
            _make_response(name=f"W{i}", tenant_id=str(mock_user["id"]))
            for i in range(21)
        ]
        mock_service.get_widgets.return_value = (widgets, 21)

        resp = client.get("/widgets/")
        data = resp.json()
        assert data["total"] == 21
        assert data["pages"] == 2

    def test_list_widgets_401_without_auth(self, client):
        from fastapi import HTTPException

        def mock_no_user():
            raise HTTPException(status_code=401, detail="Access token required")

        app.dependency_overrides[get_current_user] = mock_no_user
        try:
            response = client.get("/widgets/")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_list_widgets_tenant_isolation(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        mock_service.get_widgets.return_value = ([], 0)

        response = client.get("/widgets/")
        assert response.status_code == 200

        mock_service.get_widgets.assert_called_once()
        call_kwargs = mock_service.get_widgets.call_args.kwargs
        assert call_kwargs["tenant_id"] == str(mock_user["id"])


class TestCreateWidget:
    def test_create_widget_returns_201(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        widget = _make_response(
            name="New Widget",
            domain="https://myshop.com",
            tenant_id=str(mock_user["id"]),
        )
        mock_service.create_widget.return_value = widget

        response = client.post(
            "/widgets/",
            json={
                "name": "New Widget",
                "domain": "https://myshop.com",
                "config": {
                    "brand_color": "#2563eb",
                    "button_text": "Get a Quote",
                    "fields": ["name", "email"],
                    "success_message": "Thanks!",
                },
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Widget"

    def test_create_widget_422_invalid(self, client, _mock_auth_and_service):
        response = client.post(
            "/widgets/",
            json={"name": "", "domain": "not-a-url"},
        )
        assert response.status_code == 422

    def test_create_widget_409_duplicate(self, client, _mock_auth_and_service):
        mock_service, _ = _mock_auth_and_service
        from fastapi import HTTPException, status

        mock_service.create_widget.side_effect = HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A widget with this domain already exists",
        )

        response = client.post(
            "/widgets/",
            json={
                "name": "Duplicate",
                "domain": "https://myshop.com",
            },
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_create_widget_401_without_auth(self, client):
        from fastapi import HTTPException

        def mock_no_user():
            raise HTTPException(status_code=401, detail="Access token required")

        app.dependency_overrides[get_current_user] = mock_no_user
        try:
            response = client.post(
                "/widgets/",
                json={"name": "Test", "domain": "https://test.com"},
            )
            assert response.status_code == 401
        finally:
            app.dependency_overrides.pop(get_current_user, None)


class TestGetWidget:
    def test_get_widget_200(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        widget_id = uuid.uuid4()
        widget = _make_response(
            id=str(widget_id),
            name="Detail Widget",
            tenant_id=str(mock_user["id"]),
        )
        mock_service.get_widget.return_value = widget

        response = client.get(f"/widgets/{widget_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Detail Widget"

    def test_get_widget_404(self, client, _mock_auth_and_service):
        mock_service, _ = _mock_auth_and_service
        mock_service.get_widget.return_value = None

        response = client.get(f"/widgets/{uuid.uuid4()}")
        assert response.status_code == 404

    # Cross-tenant reads deliberately return 404 (not 403) so existence is not
    # leaked to other tenants; the test name predates that leak-safe behavior.
    def test_get_widget_403_wrong_tenant(self, client, _mock_auth_and_service):
        mock_service, _mock_user = _mock_auth_and_service
        mock_service.get_widget.return_value = None

        response = client.get(f"/widgets/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_get_widget_401_without_auth(self, client):
        from fastapi import HTTPException

        def mock_no_user():
            raise HTTPException(status_code=401, detail="Access token required")

        app.dependency_overrides[get_current_user] = mock_no_user
        try:
            response = client.get(f"/widgets/{uuid.uuid4()}")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_get_widget_invalid_uuid(self, client, _mock_auth_and_service):
        response = client.get("/widgets/abc")
        assert response.status_code == 422


class TestGetWidgetEmbed:
    def test_get_widget_embed_200(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        widget_id = uuid.uuid4()
        widget = _make_response(
            id=str(widget_id),
            name="Embed Widget",
            tenant_id=str(mock_user["id"]),
            js_version=3,
        )
        mock_service.get_widget.return_value = widget

        response = client.get(f"/widgets/{widget_id}/embed")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert (
            f'src="http://testserver/public/widget/{widget_id}/widget.js?v=3"' in body
        )
        assert f'data-widget-id="{widget_id}"' in body
        assert "defer" in body

    def test_get_widget_embed_404(self, client, _mock_auth_and_service):
        mock_service, _ = _mock_auth_and_service
        mock_service.get_widget.return_value = None

        response = client.get(f"/widgets/{uuid.uuid4()}/embed")
        assert response.status_code == 404

    def test_get_widget_embed_401_without_auth(self, client):
        from fastapi import HTTPException

        def mock_no_user():
            raise HTTPException(status_code=401, detail="Access token required")

        app.dependency_overrides[get_current_user] = mock_no_user
        try:
            response = client.get(f"/widgets/{uuid.uuid4()}/embed")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.pop(get_current_user, None)


class TestUpdateWidget:
    def test_update_widget_200(self, client, _mock_auth_and_service):
        mock_service, mock_user = _mock_auth_and_service
        widget_id = uuid.uuid4()
        widget = _make_response(
            id=str(widget_id),
            name="Updated",
            tenant_id=str(mock_user["id"]),
        )
        mock_service.update_widget.return_value = widget

        response = client.put(
            f"/widgets/{widget_id}",
            json={"name": "Updated"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated"

    def test_update_widget_404(self, client, _mock_auth_and_service):
        mock_service, _ = _mock_auth_and_service
        mock_service.update_widget.return_value = None

        response = client.put(
            f"/widgets/{uuid.uuid4()}",
            json={"name": "Nope"},
        )
        assert response.status_code == 404

    def test_update_widget_422(self, client, _mock_auth_and_service):
        response = client.put(
            f"/widgets/{uuid.uuid4()}",
            json={"name": ""},
        )
        assert response.status_code == 422

    def test_update_widget_401_without_auth(self, client):
        from fastapi import HTTPException

        def mock_no_user():
            raise HTTPException(status_code=401, detail="Access token required")

        app.dependency_overrides[get_current_user] = mock_no_user
        try:
            response = client.put(
                f"/widgets/{uuid.uuid4()}",
                json={"name": "Test"},
            )
            assert response.status_code == 401
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_update_widget_invalid_uuid(self, client, _mock_auth_and_service):
        response = client.put("/widgets/abc", json={"name": "Test"})
        assert response.status_code == 422


class TestDeleteWidget:
    def test_delete_widget_204(self, client, _mock_auth_and_service):
        mock_service, _ = _mock_auth_and_service
        mock_service.delete_widget.return_value = True

        response = client.delete(f"/widgets/{uuid.uuid4()}")
        assert response.status_code == 204
        assert response.content == b""

    def test_delete_widget_404(self, client, _mock_auth_and_service):
        mock_service, _ = _mock_auth_and_service
        mock_service.delete_widget.return_value = False

        response = client.delete(f"/widgets/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_delete_widget_401_without_auth(self, client):
        from fastapi import HTTPException

        def mock_no_user():
            raise HTTPException(status_code=401, detail="Access token required")

        app.dependency_overrides[get_current_user] = mock_no_user
        try:
            response = client.delete(f"/widgets/{uuid.uuid4()}")
            assert response.status_code == 401
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_delete_widget_invalid_uuid(self, client, _mock_auth_and_service):
        response = client.delete("/widgets/abc")
        assert response.status_code == 422
