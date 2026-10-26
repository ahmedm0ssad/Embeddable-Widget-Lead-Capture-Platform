import json

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


SCHEMA_FIELDS = {"category", "summary", "quality_flags", "confidence", "reason"}
_VALID_CONTENT = json.dumps(
    {
        "category": "fiction",
        "summary": "A summary.",
        "quality_flags": [],
        "confidence": 0.9,
        "reason": "A reason.",
    }
)
_BAD_CONTENT = json.dumps(
    {
        "category": "cooking",
        "summary": "A summary.",
        "quality_flags": [],
        "confidence": 0.9,
        "reason": "A reason.",
    }
)


class _ScriptedProvider:
    """Canned provider replies in order, for driving the real pipeline offline."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    def complete(self, *, model, system_prompt, user_content, temperature):
        from app.services.llm.provider import LLMResponse

        self.calls += 1
        return LLMResponse(content=self.contents.pop(0), model=model)


class TestStage3SchemaContract:
    """Stage 3: the endpoint contract is schema-only again (no "raw" key)."""

    def test_success_response_body_has_only_schema_fields(self, client, monkeypatch):
        from app.models.book_classify import BookCategory, ClassifyOutput, QualityFlag

        output = ClassifyOutput(
            category=BookCategory.FICTION,
            summary="A summary.",
            quality_flags=[QualityFlag.SHORT_DESCRIPTION],
            confidence=0.5,
            reason="A reason.",
        )
        monkeypatch.setattr(
            "app.routers.books_classify.classify_book", lambda *a, **kw: output
        )

        response = _post(client, {"title": "T", "description": "d"})

        assert response.status_code == 200
        data = response.json()
        assert set(data) == SCHEMA_FIELDS
        assert "raw" not in data

    def test_repaired_success_returns_schema_only(self, client, monkeypatch, tmp_path):
        """Bad first answer, good repair → 200 with ONLY schema fields."""
        from app.services.llm import cache, pipeline

        cache.clear()
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(pipeline, "QUARANTINE_LOG_PATH", tmp_path / "q.jsonl")
        scripted = _ScriptedProvider([_BAD_CONTENT, _VALID_CONTENT])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

        response = _post(client, {"title": "Dune", "description": "A description."})

        assert response.status_code == 200
        assert scripted.calls == 2  # initial + the one repair
        data = response.json()
        assert set(data) == SCHEMA_FIELDS  # no "raw" key, never raw model text
        assert not (tmp_path / "q.jsonl").exists()  # recovered: no quarantine line
        cache.clear()

    def test_double_failure_maps_to_422_through_http_layer(
        self, client, monkeypatch, tmp_path
    ):
        """Broken twice → exactly one repair → 422 with a readable detail."""
        from app.services.llm import cache, pipeline

        cache.clear()
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        quarantine_path = tmp_path / "q.jsonl"
        monkeypatch.setattr(pipeline, "QUARANTINE_LOG_PATH", quarantine_path)
        scripted = _ScriptedProvider([_BAD_CONTENT, _BAD_CONTENT])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

        response = _post(client, {"title": "Dune", "description": "A description."})

        assert response.status_code == 422
        assert "failed schema validation" in response.json()["detail"]
        assert scripted.calls == 2  # exactly one repair attempt
        lines = [
            json.loads(line)
            for line in quarantine_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(lines) == 1
        assert lines[0]["attempt"] == "repair"
        assert lines[0]["input"] == {"title": "Dune", "description": "A description."}
        assert lines[0]["model"] == "test-model"
        cache.clear()
