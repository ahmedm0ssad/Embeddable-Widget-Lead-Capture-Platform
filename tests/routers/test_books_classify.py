import pytest
from fastapi.testclient import TestClient

CATEGORIES = {
    "fiction",
    "non-fiction",
    "mystery-thriller",
    "sci-fi-fantasy",
    "romance",
    "horror",
    "biography",
    "self-help",
    "other",
}
FLAGS = {
    "missing_description",
    "short_description",
    "vague_title",
    "conflicting_metadata",
}

VALID_DESCRIPTION = (
    "A novel about a woman who explores the infinite versions of her life "
    "between life and death, choosing whether to live or move on."
)


@pytest.fixture(autouse=True)
def stub_mode(monkeypatch):
    """Every test runs with LLM_STUB=1 unless it explicitly removes the var."""
    monkeypatch.setenv("LLM_STUB", "1")
    yield


def _post(client, payload):
    return client.post("/books/classify", json=payload)


class TestStubMode:
    def test_valid_request_returns_200_with_full_schema(self, client: TestClient):
        response = _post(
            client,
            {"title": "The Midnight Library", "description": VALID_DESCRIPTION},
        )

        assert response.status_code == 200
        data = response.json()
        assert set(data) == {
            "category",
            "summary",
            "quality_flags",
            "confidence",
            "reason",
        }
        assert data["category"] in CATEGORIES
        assert isinstance(data["summary"], str) and len(data["summary"]) <= 200
        assert isinstance(data["quality_flags"], list)
        assert all(f in FLAGS for f in data["quality_flags"])
        assert 0.0 <= data["confidence"] <= 1.0
        assert isinstance(data["reason"], str) and data["reason"]

    def test_missing_description_sets_flag(self, client: TestClient):
        response = _post(client, {"title": "Unknown Book"})

        assert response.status_code == 200
        data = response.json()
        assert data["quality_flags"] == ["missing_description"]

    def test_empty_description_sets_flag(self, client: TestClient):
        response = _post(client, {"title": "Unknown Book", "description": "   "})

        assert response.status_code == 200
        assert response.json()["quality_flags"] == ["missing_description"]

    def test_result_is_deterministic(self, client: TestClient):
        payload = {"title": "Dune", "description": VALID_DESCRIPTION}
        first = _post(client, payload).json()
        second = _post(client, payload).json()
        assert first == second


class TestValidation400:
    """Manual validation — violations must be 400 naming the field, never 422."""

    def test_missing_title(self, client: TestClient):
        response = _post(client, {"description": VALID_DESCRIPTION})

        assert response.status_code == 400
        assert "title" in response.json()["detail"]["fields"]

    def test_empty_dict_missing_title(self, client: TestClient):
        response = _post(client, {})

        assert response.status_code == 400
        assert "title" in response.json()["detail"]["fields"]

    def test_empty_title(self, client: TestClient):
        response = _post(client, {"title": ""})

        assert response.status_code == 400
        assert "title" in response.json()["detail"]["fields"]

    def test_title_too_long(self, client: TestClient):
        response = _post(client, {"title": "x" * 501})

        assert response.status_code == 400
        assert "title" in response.json()["detail"]["fields"]

    def test_non_string_title(self, client: TestClient):
        response = _post(client, {"title": 12345})

        assert response.status_code == 400
        assert "title" in response.json()["detail"]["fields"]

    def test_description_too_long_skips_service(self, client, monkeypatch):
        called = []

        def _spy(*args, **kwargs):
            called.append(True)
            raise AssertionError("service must not be called on validation failure")

        monkeypatch.setattr("app.routers.books_classify.classify_book", _spy)

        response = _post(client, {"title": "Fine", "description": "d" * 2001})

        assert response.status_code == 400
        assert "description" in response.json()["detail"]["fields"]
        assert called == []

    def test_missing_body(self, client: TestClient):
        response = client.post("/books/classify")

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "validation_failed"

    def test_non_object_body(self, client: TestClient):
        response = client.post("/books/classify", json=["not", "an", "object"])

        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "validation_failed"


class TestStubNotConfigured:
    def test_stub_mode_off_returns_503(self, client, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)

        response = _post(client, {"title": "The Midnight Library"})

        assert response.status_code == 503
        assert "LLM integration not configured" in response.json()["detail"]
