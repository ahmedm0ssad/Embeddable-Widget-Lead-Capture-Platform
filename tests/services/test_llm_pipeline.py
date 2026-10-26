"""Offline tests for the A17 Stage 2 LLM package.

Covers prompt loading from the real committed prompt file, the in-process
cache (stale-busting via prompt version, clear()), the provider interface
(stub determinism, OpenAICompat config errors — constructed but never called
with network), and the pipeline (stub bypass, delegation, cache hit, 503
mapping). No test imports ``openai`` or touches the network.
"""

import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.models.book_classify import BookCategory, ClassifyOutput, QualityFlag
from app.services import book_classify_service
from app.services.llm import cache, pipeline
from app.services.llm.prompt_loader import PromptFormatError, load_prompt
from app.services.llm.provider import (
    DEFAULT_LLM_MODEL,
    LLMConfigError,
    OpenAICompatProvider,
    StubProvider,
)

STUB_CONTENT = json.dumps(
    {
        "category": "fiction",
        "summary": "Stub summary.",
        "quality_flags": [],
        "confidence": 0.9,
        "reason": "Stub reason.",
    }
)


class CountingProvider:
    """Test double wrapping a provider and counting complete() calls."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def complete(self, *, model, system_prompt, user_content, temperature):
        self.calls += 1
        return self.inner.complete(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
        )


class TestPromptLoader:
    def test_loads_real_committed_prompt_file(self):
        version, system_prompt = load_prompt(Path(pipeline.BOOK_CLASSIFY_PROMPT))

        assert version == "book-classify-v1"
        assert system_prompt

    def test_system_prompt_contains_expected_marker_sections(self):
        _, system_prompt = load_prompt(pipeline.BOOK_CLASSIFY_PROMPT)

        assert "Role and job" in system_prompt
        assert "What to do when unsure" in system_prompt
        # Design notes below the END marker must not leak into the prompt.
        assert "Design notes" not in system_prompt

    def test_missing_version_marker_raises(self, tmp_path: Path):
        path = tmp_path / "no-version.md"
        path.write_text("body only", encoding="utf-8")

        with pytest.raises(PromptFormatError, match="prompt-version"):
            load_prompt(path)

    def test_missing_start_marker_raises(self, tmp_path: Path):
        path = tmp_path / "no-start.md"
        path.write_text(
            "<!-- prompt-version: x-v1 -->\nno markers here", encoding="utf-8"
        )

        with pytest.raises(PromptFormatError, match="SYSTEM-PROMPT-START"):
            load_prompt(path)

    def test_missing_end_marker_raises(self, tmp_path: Path):
        path = tmp_path / "no-end.md"
        path.write_text(
            "<!-- prompt-version: x-v1 -->\n"
            "<!---- SYSTEM-PROMPT-START ---->\n"
            "unclosed body",
            encoding="utf-8",
        )

        with pytest.raises(PromptFormatError, match="SYSTEM-PROMPT-END"):
            load_prompt(path)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(PromptFormatError, match="unreadable"):
            load_prompt(tmp_path / "does-not-exist.md")


class TestCache:
    def test_same_inputs_same_key(self):
        payload = {"title": "Dune", "description": None}
        first = cache.cache_key("book-classify-v1", "m1", payload)
        second = cache.cache_key("book-classify-v1", "m1", payload)

        assert first == second

    def test_key_differs_when_prompt_version_differs(self):
        payload = {"title": "Dune", "description": None}
        v1 = cache.cache_key("book-classify-v1", "m1", payload)
        v2 = cache.cache_key("book-classify-v2", "m1", payload)

        assert v1 != v2

    def test_key_differs_when_model_or_payload_differs(self):
        payload = {"title": "Dune", "description": None}
        assert cache.cache_key("v", "m1", payload) != cache.cache_key(
            "v", "m2", payload
        )
        assert cache.cache_key("v", "m1", payload) != cache.cache_key(
            "v", "m1", {"title": "Other", "description": None}
        )

    def test_put_get_and_clear(self):
        cache.clear()

        cache.put("k1", "v1")
        assert cache.get("k1") == "v1"
        assert cache.get("other") is None

        cache.clear()
        assert cache.get("k1") is None


class TestStubProvider:
    def test_deterministic_regardless_of_arguments(self):
        provider = StubProvider()
        first = provider.complete(
            model="any", system_prompt="s", user_content="u", temperature=0.7
        )
        second = provider.complete(
            model="other", system_prompt="ignored", user_content="data", temperature=1.0
        )

        assert first == second
        assert first.content == provider.content
        assert first.model == "stub"

    def test_canned_content_is_schema_correct_json(self):
        provider = StubProvider()
        response = provider.complete(
            model="any", system_prompt="s", user_content="u", temperature=0.0
        )

        parsed = json.loads(response.content)
        assert set(parsed) == {
            "category",
            "summary",
            "quality_flags",
            "confidence",
            "reason",
        }
        assert parsed["category"] == "fiction"
        assert parsed["quality_flags"] == []


class TestOpenAICompatProviderConfig:
    def test_importable_offline(self):
        # Constructing must be side-effect free: no openai import, no network.
        provider = OpenAICompatProvider()
        assert provider._client is None
        assert "openai" not in sys.modules

    def test_complete_raises_clear_error_naming_missing_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)

        provider = OpenAICompatProvider()
        with pytest.raises(LLMConfigError) as exc_info:
            provider.complete(
                model="m", system_prompt="s", user_content="u", temperature=0.0
            )

        message = str(exc_info.value)
        assert "LLM_BASE_URL" in message
        assert "LLM_API_KEY" in message

    def test_actual_model_is_resolved_explicit_model_first(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "env-model")
        provider = OpenAICompatProvider(base_url="http://unused", api_key="unused")
        captured: dict = {}

        # Minimal SDK stand-in matching the client.chat.completions.create path.
        message = types.SimpleNamespace(content="{}")
        choice = types.SimpleNamespace(message=message)
        completions = types.SimpleNamespace()

        def create(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(choices=[choice])

        completions.create = create
        provider._client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )

        provider.complete(
            model="explicit-model",
            system_prompt="s",
            user_content="u",
            temperature=0.0,
        )

        assert captured["model"] == "explicit-model"
        assert len(captured["messages"]) == 2
        assert captured["messages"][0]["role"] == "system"
        assert captured["messages"][1]["role"] == "user"
        assert captured["temperature"] == 0.0

    def test_default_model_constant_matches_env_example(self):
        # Groq rotated models; the default must be the live id.
        assert DEFAULT_LLM_MODEL == "openai/gpt-oss-20b"


class TestPipelineStubPath:
    def test_classify_book_delegates_to_pipeline(self, monkeypatch):
        monkeypatch.setenv("LLM_STUB", "1")
        provider = CountingProvider(StubProvider())
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        result = book_classify_service.classify_book("Dune", "A good description.")

        assert isinstance(result, ClassifyOutput)
        assert result.category is BookCategory.FICTION
        assert result.quality_flags == []
        assert provider.calls == 0  # stub path never consults a provider

    def test_stub_missing_description_sets_flag(self, monkeypatch):
        monkeypatch.setenv("LLM_STUB", "1")
        provider = CountingProvider(StubProvider())
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        result = book_classify_service.classify_book("Untitled Thing", None)

        assert result.quality_flags == [QualityFlag.MISSING_DESCRIPTION]
        assert provider.calls == 0

    def test_stub_bypasses_cache_too(self, monkeypatch):
        cache.clear()
        monkeypatch.setenv("LLM_STUB", "1")
        provider = CountingProvider(StubProvider())
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        book_classify_service.classify_book("Dune", None)

        assert provider.calls == 0
        assert cache._CACHE == {}


class TestPipelineRealPathOffline:
    """Real-mode behavior with the provider injected — no network anywhere."""

    @pytest.fixture(autouse=True)
    def _cache_and_env(self, monkeypatch):
        cache.clear()
        monkeypatch.setenv("LLM_STUB", "1")  # removed per-test as needed
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        yield
        cache.clear()

    def _inject(self, monkeypatch, content=STUB_CONTENT):
        counter = CountingProvider(StubProvider(content=content))
        monkeypatch.setattr(pipeline, "_get_provider", lambda: counter)
        return counter

    def test_returns_raw_text_and_calls_provider_once(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        counter = self._inject(monkeypatch, content='{"raw-model-json": true}')

        result = book_classify_service.classify_book("Dune", "A description.")

        assert isinstance(result, pipeline.RawClassifyOutput)
        assert result.raw == '{"raw-model-json": true}'
        assert counter.calls == 1

    def test_second_same_input_hits_cache_without_provider_call(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        counter = self._inject(monkeypatch, content="segment-one")

        first = book_classify_service.classify_book("Dune", "A description.")
        second = book_classify_service.classify_book("Dune", "A description.")

        assert first.raw == second.raw == "segment-one"
        assert counter.calls == 1  # second call served from cache

    def test_different_input_misses_cache(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        counter = self._inject(monkeypatch, content="segment-two")

        book_classify_service.classify_book("Dune", "A description.")
        book_classify_service.classify_book("Dune", "A different description.")

        assert counter.calls == 2

    def test_user_content_is_json_encoded_user_message(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        seen = {}

        class CapturingProvider:
            def complete(self, *, model, system_prompt, user_content, temperature):
                seen.update(
                    model=model,
                    system_prompt=system_prompt,
                    user_content=user_content,
                    temperature=temperature,
                )
                return StubProvider().complete(
                    model=model,
                    system_prompt=system_prompt,
                    user_content=user_content,
                    temperature=temperature,
                )

        monkeypatch.setattr(pipeline, "_get_provider", lambda: CapturingProvider())
        book_classify_service.classify_book('Malice "\b\n Title', "Desc")

        # Anti-injection contract: user data JSON-encoded, out of system prompt.
        assert json.loads(seen["user_content"]) == {
            "title": 'Malice "\b\n Title',
            "description": "Desc",
        }
        assert '"Malice' not in seen["system_prompt"]
        assert seen["model"] == "test-model"
        assert seen["temperature"] == 0.0

    def test_llm_enabled_off_returns_503(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "")
        self._inject(monkeypatch)

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", None)

        assert exc_info.value.status_code == 503
        assert "LLM integration not configured" in exc_info.value.detail

    def test_missing_credentials_map_to_503(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.delenv("LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        # No injection: _get_provider returns a real OpenAICompatProvider whose
        # complete() raises LLMConfigError before any network/import happens.

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", None)

        assert exc_info.value.status_code == 503
        assert "LLM integration not configured" in exc_info.value.detail

    def test_provider_failure_maps_to_503_never_500(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)

        class FailingProvider:
            def complete(self, **kwargs):
                raise OSError("network down")

        monkeypatch.setattr(pipeline, "_get_provider", lambda: FailingProvider())

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", None)

        assert exc_info.value.status_code == 503
        assert "LLM provider call failed" in exc_info.value.detail

    def test_default_model_used_when_llm_model_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        seen = {}

        class CapturingProvider:
            def complete(self, *, model, system_prompt, user_content, temperature):
                seen["model"] = model
                return StubProvider().complete(
                    model=model,
                    system_prompt=system_prompt,
                    user_content=user_content,
                    temperature=temperature,
                )

        monkeypatch.setattr(pipeline, "_get_provider", lambda: CapturingProvider())
        book_classify_service.classify_book("Dune", "Desc")

        assert seen["model"] == DEFAULT_LLM_MODEL
