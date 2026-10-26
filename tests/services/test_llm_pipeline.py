"""Offline tests for the A17 LLM package (Stage 4: fit to run in production).

Covers prompt loading from the real committed prompt file, the in-process
cache (stale-busting via prompt version, clear/drop), the provider interface
(stub determinism, OpenAICompat config errors — constructed but never called
with network), the raw-text parser, schema validation (collect ALL errors,
deterministic flag order), the pipeline: parse → validate → exactly one
repair retry → quarantine + 422, cache-hit parsing, the Stage 4 transport
retry policy (timeouts, retryable vs fail-fast, Retry-After, backoff —
real openai exception types raised by a fake SDK client, ``_sleep``
recorded, no I/O), the ``llm_call`` cost line (caplog), and the
``LLM_ENABLED=false`` kill switch. No test touches the network; ``openai``
is imported only lazily inside test helpers.
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
from app.services.llm import provider as provider_module
from app.services.llm.parser import (
    RawParseError,
    parse_and_validate,
    parse_json_object,
    validate_classify_payload,
)
from app.services.llm.prompt_loader import PromptFormatError, load_prompt
from app.services.llm.provider import (
    DEFAULT_LLM_MODEL,
    LLMClientError,
    LLMConfigError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
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

_REPAIR_INSTRUCTION = (
    "Your previous answer was rejected for this reason. Return only corrected "
    "JSON matching the schema. No commentary, no code fences."
)


def valid_payload(**overrides):
    """A schema-valid classify payload dict, overridable per test."""
    payload = {
        "category": "fiction",
        "summary": "A plain summary of a plain book.",
        "quality_flags": [],
        "confidence": 0.9,
        "reason": "It reads as a straightforward narrative.",
    }
    payload.update(overrides)
    return payload


def valid_content(**overrides) -> str:
    """A schema-valid raw model reply string, overridable per test."""
    return json.dumps(valid_payload(**overrides))


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


class ScriptedProvider:
    """Returns canned replies in order; records every message it is sent."""

    def __init__(self, contents: list[str], usage: "LLMUsage | None" = None):
        self.contents = list(contents)
        self.calls = 0
        self.user_contents: list[str] = []
        self.system_prompts: list[str] = []
        self.usage = usage

    def complete(self, *, model, system_prompt, user_content, temperature):
        self.calls += 1
        self.user_contents.append(user_content)
        self.system_prompts.append(system_prompt)
        return LLMResponse(
            content=self.contents.pop(0),
            model=model,
            usage=self.usage or LLMUsage(),
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

    def test_drop_evicts_single_key(self):
        cache.clear()

        cache.put("k1", "v1")
        cache.put("k2", "v2")
        cache.drop("k1")

        assert cache.get("k1") is None
        assert cache.get("k2") == "v2"
        cache.clear()


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


class TestParser:
    def test_bare_json_parses(self):
        payload = parse_json_object(valid_content())

        assert payload["category"] == "fiction"

    def test_fenced_json_parses(self):
        fenced = "```json\n" + valid_content() + "\n```"

        payload = parse_json_object(fenced)

        assert payload["category"] == "fiction"

    def test_preamble_text_parses(self):
        preambled = "Sure! Here's the JSON you asked for:\n" + valid_content()

        payload = parse_json_object(preambled)

        assert payload["summary"] == "A plain summary of a plain book."

    def test_trailing_junk_parses(self):
        trailing = valid_content() + "\nHope that helps! Let me know if you need more."

        payload = parse_json_object(trailing)

        assert payload["confidence"] == 0.9

    def test_two_objects_takes_the_first(self):
        first = valid_content()
        second = json.dumps(valid_payload(category="horror"))
        two = first + "\n" + second

        payload = parse_json_object(two)

        assert payload["category"] == "fiction"

    def test_braces_inside_string_values_do_not_break_balance(self):
        tricky = json.dumps(
            {
                "category": "fiction",
                "summary": 'Supports braces { and "quotes" in strings \\ "etc',
                "quality_flags": [],
                "confidence": 0.5,
                "reason": "Trailed by } in text.",
            }
        )
        padded = "Note: " + tricky + " done."

        payload = parse_json_object(padded)

        assert 'braces { and "quotes" in strings' in payload["summary"]

    def test_no_object_raises_raw_parse_error(self):
        with pytest.raises(RawParseError):
            parse_json_object("The model refused to answer, sorry!")

    def test_whitespace_only_raises(self):
        with pytest.raises(RawParseError):
            parse_json_object("   \n\t  ")

    def test_truncated_object_raises(self):
        with pytest.raises(RawParseError):
            parse_json_object('{"category": "fiction", "summary": "unfini')

    def test_non_string_input_raises(self):
        with pytest.raises(RawParseError, match="not text"):
            parse_json_object(None)  # type: ignore[arg-type]

    def test_unicode_in_string_values_preserved(self):
        # Real Groq outputs contain U+2011 non-breaking hyphens inside string
        # values — legal JSON string content, must never be stripped.
        content = json.dumps(
            valid_payload(summary="Planning a twelve\u2011year\u2011old study."),
            ensure_ascii=False,
        )

        payload = parse_json_object(content)

        assert payload["summary"] == "Planning a twelve\u2011year\u2011old study."


class TestValidate:
    def test_valid_payload_returns_schema_object(self):
        output, errors = validate_classify_payload(valid_payload())

        assert errors == []
        assert output is not None
        assert output.category is BookCategory.FICTION
        assert isinstance(output, ClassifyOutput)

    def test_broken_enum_reports_error_and_rejects(self):
        output, errors = validate_classify_payload(valid_payload(category="cooking"))

        assert output is None
        assert errors
        assert any("category" in e for e in errors)

    def test_broken_enum_flag_in_list_rejects(self):
        output, errors = validate_classify_payload(
            valid_payload(quality_flags=["soup"])
        )

        assert output is None
        assert errors

    def test_missing_required_fields_all_collected(self):
        payload = valid_payload()
        del payload["summary"]
        payload.pop("reason")
        payload.pop("confidence")

        output, errors = validate_classify_payload(payload)

        assert output is None
        assert len(errors) == 3  # ALL errors, not just the first
        assert any("summary" in e for e in errors)
        assert any("reason" in e for e in errors)
        assert any("confidence" in e for e in errors)

    def test_wrong_type_rejected(self):
        output, errors = validate_classify_payload(
            valid_payload(quality_flags="missing_description")
        )

        assert output is None
        assert any("quality_flags" in e for e in errors)

    def test_out_of_range_confidence_rejected(self):
        output, errors = validate_classify_payload(valid_payload(confidence=1.7))

        assert output is None
        assert any("confidence" in e for e in errors)

    def test_extra_field_rejected(self):
        output, errors = validate_classify_payload(
            valid_payload(author_mood="delighted")
        )

        assert output is None
        assert any("author_mood" in e for e in errors)

    def test_string_number_coercion_accepted(self):
        # Documented laxness: Pydantic v2 default lax mode coerces
        # string-encoded numbers; model outputs often stringify numbers.
        output, errors = validate_classify_payload(valid_payload(confidence="0.9"))

        assert errors == []
        assert output is not None
        assert output.confidence == 0.9

    def test_summary_too_long_rejected(self):
        output, errors = validate_classify_payload(valid_payload(summary="x" * 201))

        assert output is None
        assert any("summary" in e for e in errors)

    def test_flags_sorted_deterministically(self):
        # Model flag order is unstable; sorted (alphabetical) output makes
        # equal payloads compare equal.
        output, errors = validate_classify_payload(
            valid_payload(quality_flags=["vague_title", "short_description"])
        )

        assert errors == []
        assert output is not None
        assert output.quality_flags == [
            QualityFlag.SHORT_DESCRIPTION,
            QualityFlag.VAGUE_TITLE,
        ]

    def test_flags_sorted_alphabetically_by_value(self):
        output, _ = validate_classify_payload(
            valid_payload(quality_flags=["conflicting_metadata", "vague_title"])
        )

        assert output is not None
        assert [f.value for f in output.quality_flags] == [
            "conflicting_metadata",
            "vague_title",
        ]

    def test_parse_and_validate_never_raises(self):
        output, errors = parse_and_validate("there is no json here at all")

        assert output is None
        assert errors == ["no complete JSON object found in raw model output"]


class TestPipelineRealPathOffline:
    """Real-mode behavior with the provider injected — no network anywhere."""

    @pytest.fixture(autouse=True)
    def _cache_and_env(self, monkeypatch, tmp_path):
        cache.clear()
        monkeypatch.setenv("LLM_STUB", "1")  # removed per-test as needed
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(
            pipeline, "QUARANTINE_LOG_PATH", tmp_path / "quarantine.jsonl"
        )
        self._quarantine_path = tmp_path / "quarantine.jsonl"
        yield
        cache.clear()

    def quarantine_lines(self) -> list[dict]:
        if not self._quarantine_path.exists():
            return []
        return [
            json.loads(line)
            for line in self._quarantine_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_returns_validated_output_and_calls_provider_once(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        scripted = ScriptedProvider([valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

        result = book_classify_service.classify_book("Dune", "A description.")

        assert isinstance(result, ClassifyOutput)
        assert result.category is BookCategory.FICTION
        assert scripted.calls == 1
        assert self.quarantine_lines() == []  # success writes no quarantine line

    def test_cache_stores_raw_text_not_parsed_output(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        scripted = ScriptedProvider([valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)
        payload = {"title": "Dune", "description": "A description."}
        key = cache.cache_key("book-classify-v1", "test-model", payload)

        book_classify_service.classify_book(**payload)

        stored = cache.get(key)
        assert stored is not None
        assert json.loads(stored) == valid_payload()  # raw string, not an object

    def test_second_same_input_hits_cache_without_provider_call(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        scripted = ScriptedProvider([valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

        first = book_classify_service.classify_book("Dune", "A description.")
        second = book_classify_service.classify_book("Dune", "A description.")

        assert first == second  # both validated schema outputs
        assert isinstance(second, ClassifyOutput)  # cache hit re-parses, no raw leak
        assert scripted.calls == 1  # second call served from cache

    def test_different_input_misses_cache(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        scripted = ScriptedProvider([valid_content(), valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

        book_classify_service.classify_book("Dune", "A description.")
        book_classify_service.classify_book("Dune", "A different description.")

        assert scripted.calls == 2

    def test_user_content_is_json_encoded_user_message(self, monkeypatch):
        monkeypatch.delenv("LLM_STUB", raising=False)
        scripted = ScriptedProvider([valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)
        book_classify_service.classify_book('Malice "\b\n Title', "Desc")

        # Anti-injection contract: user data JSON-encoded, out of system prompt.
        seen_user = json.loads(scripted.user_contents[0])
        assert seen_user == {"title": 'Malice "\b\n Title', "description": "Desc"}
        assert '"Malice' not in scripted.system_prompts[0]

    def test_llm_enabled_blank_or_unset_returns_503(self, monkeypatch):
        # Blank/unset is "not configured", NOT the kill switch (an empty env
        # line must never silently disable the model) — kill switch requires
        # an explicit falsy value like "false"/"0"/"no"/"off".
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "")
        scripted = ScriptedProvider([valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

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
        scripted = ScriptedProvider([valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)
        book_classify_service.classify_book("Dune", "Desc")

        assert scripted.calls == 1


class TestPipelineRepairFlow:
    """Stage 3 repair-once→quarantine chain, provider scripted, fully offline."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch, tmp_path):
        cache.clear()
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(
            pipeline, "QUARANTINE_LOG_PATH", tmp_path / "quarantine.jsonl"
        )
        self._quarantine_path = tmp_path / "quarantine.jsonl"
        yield
        cache.clear()

    def quarantine_lines(self) -> list[dict]:
        assert self._quarantine_path.exists(), "quarantine file missing"
        return [
            json.loads(line)
            for line in self._quarantine_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _inject(self, monkeypatch, contents) -> ScriptedProvider:
        scripted = ScriptedProvider(contents)
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)
        return scripted

    def test_broken_enum_triggers_one_repair_then_success(self, monkeypatch):
        bad = json.dumps(valid_payload(category="cooking"))
        scripted = self._inject(monkeypatch, [bad, valid_content(category="romance")])

        result = book_classify_service.classify_book("A Love Letter", "Romance plot.")

        assert result.category is BookCategory.ROMANCE
        assert scripted.calls == 2  # initial + exactly one repair

        # Repair message: original content + previous answer + exact errors.
        repair_user = json.loads(scripted.user_contents[1])
        assert repair_user["original_record"] == {
            "title": "A Love Letter",
            "description": "Romance plot.",
        }
        assert json.loads(repair_user["previous_answer"]) == valid_payload(
            category="cooking"
        )
        assert any("cooking" in reason for reason in repair_user["rejection_reasons"])
        assert repair_user["instruction"] == _REPAIR_INSTRUCTION

    def test_broken_enum_both_times_fails_422_with_one_quarantine_line(
        self, monkeypatch
    ):
        bad = json.dumps(valid_payload(category="cooking"))
        scripted = self._inject(monkeypatch, [bad, bad])

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 422
        assert "failed schema validation" in exc_info.value.detail
        assert scripted.calls == 2  # never a second repair

        lines = self.quarantine_lines()
        assert len(lines) == 1
        line = lines[0]
        assert line["attempt"] == "repair"
        assert line["input"] == {"title": "Dune", "description": "Desc."}
        assert line["prompt_version"] == "book-classify-v1"
        assert line["model"] == "test-model"
        assert any("cooking" in e for e in line["errors"])
        assert json.loads(line["raw_output"]) == valid_payload(category="cooking")

    def test_initial_failure_recovered_by_repair_writes_no_quarantine_line(
        self, monkeypatch
    ):
        bad = json.dumps(valid_payload(category="cooking"))
        scripted = self._inject(monkeypatch, [bad, valid_content()])

        book_classify_service.classify_book("Dune", "Desc.")

        assert scripted.calls == 2
        assert not self._quarantine_path.exists()

    def test_missing_required_field_same_repair_then_422_flow(self, monkeypatch):
        broken = json.dumps(
            {k: v for k, v in valid_payload().items() if k != "summary"}
        )
        still_broken = json.dumps(
            {k: v for k, v in valid_payload().items() if k != "summary"}
        )
        scripted = self._inject(monkeypatch, [broken, still_broken])

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 422
        assert scripted.calls == 2
        line = self.quarantine_lines()[0]
        assert line["attempt"] == "repair"
        assert any("summary" in e for e in line["errors"])

    def test_wrong_type_rejected_via_repair_chain(self, monkeypatch):
        wrong_type = json.dumps(valid_payload(quality_flags="missing_description"))
        scripted = self._inject(
            monkeypatch, [wrong_type, valid_content(quality_flags=["vague_title"])]
        )

        result = book_classify_service.classify_book("Dune", "Desc.")

        assert result.quality_flags == [QualityFlag.VAGUE_TITLE]
        assert scripted.calls == 2

    def test_string_number_confidence_coerces_without_repair(self, monkeypatch):
        # Documented laxness: confidence "0.9" (string) is valid via Pydantic
        # lax coercion on the FIRST attempt — no repair call burned.
        coerced = json.dumps(valid_payload(confidence="0.9"))
        scripted = self._inject(monkeypatch, [coerced])

        result = book_classify_service.classify_book("Dune", "Desc.")

        assert result.confidence == 0.9
        assert scripted.calls == 1

    def test_fenced_and_preambled_outputs_validate_fine(self, monkeypatch):
        fenced = "```json\n" + valid_content() + "\n```"

        class Delegating:
            def __init__(self, content):
                self.content = content

            def complete(self, *, model, system_prompt, user_content, temperature):
                return LLMResponse(content=self.content, model="m")

        provider = CountingProvider(Delegating(fenced))
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)

        result = book_classify_service.classify_book("Dune", "Desc.")

        assert isinstance(result, ClassifyOutput)
        assert provider.calls == 1

    def test_flags_sorted_regardless_of_model_order(self, monkeypatch):
        unordered = json.dumps(
            valid_payload(quality_flags=["vague_title", "short_description"])
        )
        self._inject(monkeypatch, [unordered])

        result = book_classify_service.classify_book("Dune", "Desc.")

        assert [f.value for f in result.quality_flags] == [
            "short_description",
            "vague_title",
        ]

    def test_unicode_string_content_preserved_end_to_end(self, monkeypatch):
        unicode_body = json.dumps(
            valid_payload(summary="A twelve\u2011year\u2011old case reopened."),
            ensure_ascii=False,
        )
        self._inject(monkeypatch, [unicode_body])

        result = book_classify_service.classify_book("Dune", "Desc.")

        assert result.summary == "A twelve\u2011year\u2011old case reopened."

    def test_repair_success_result_is_cached(self, monkeypatch):
        bad = json.dumps(valid_payload(category="cooking"))
        scripted = self._inject(monkeypatch, [bad, valid_content()])

        first = book_classify_service.classify_book("Dune", "Desc.")
        second = book_classify_service.classify_book("Dune", "Desc.")

        assert first == second
        assert scripted.calls == 2  # third equivalent call would be a cache hit

    def test_cache_hit_failure_poisoned_entry(self, monkeypatch):
        scripted = self._inject(monkeypatch, [valid_content()])
        payload = {"title": "Dune", "description": "Desc."}
        key = cache.cache_key("book-classify-v1", "test-model", payload)
        book_classify_service.classify_book(**payload)
        assert scripted.calls == 1

        # Simulate a cache entry that no longer validates (shouldn't happen;
        # e.g. schema tightening). Must quarantine once with cache-hit attempt,
        # evict the entry, and never consult the provider.
        cache.put(key, "this is { not json")
        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book(**payload)

        assert exc_info.value.status_code == 422
        assert scripted.calls == 1  # poisoned entry handled without model call
        line = self.quarantine_lines()[0]
        assert line["attempt"] == "cache-hit"
        assert line["raw_output"] == "this is { not json"
        assert cache.get(key) is None  # evicted


def _ok_sdk_response(content: str, input_tokens: int = 9, output_tokens: int = 4):
    import types

    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    usage = types.SimpleNamespace(
        prompt_tokens=input_tokens, completion_tokens=output_tokens
    )
    return types.SimpleNamespace(choices=[choice], usage=usage)


class TestProviderRetryPolicy:
    """Transport retry policy inside OpenAICompatProvider — Stage 4.

    Real ``openai`` exception types are raised by a fake SDK client (offline;
    no network), ``_sleep`` is recorded instead of sleeping, and jitter is
    pinned to its bounds for deterministic assertions.
    """

    @staticmethod
    def _sdk_error(kind: str, status_code: int = 0, headers: dict | None = None):
        import httpx
        import openai

        request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
        if kind == "timeout":
            return openai.APITimeoutError(request)
        response = httpx.Response(
            status_code, headers=dict(headers or {}), request=request
        )
        body = {"error": {"message": "synthetic"}}
        if kind == "rate":
            return openai.RateLimitError("rate limited", response=response, body=body)
        if kind == "server":
            return openai.InternalServerError("internal", response=response, body=body)
        if kind == "auth":
            return openai.AuthenticationError("bad key", response=response, body=body)
        return openai.APIStatusError(
            f"status {status_code}", response=response, body=body
        )

    class _FakeClient:
        """Scripted SDK stand-in: raises/returns items in order."""

        def __init__(self, script):
            import types

            self.chat = types.SimpleNamespace(completions=self)
            self.script = list(script)
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    def _make(self, monkeypatch, script):
        """Provider with a scripted fake client; jitter pinned high."""
        sleeps: list[float] = []
        monkeypatch.setattr(provider_module, "_sleep", sleeps.append)
        monkeypatch.setattr(
            provider_module,
            "random",
            types.SimpleNamespace(uniform=lambda lo, hi: hi),
        )
        provider = OpenAICompatProvider(
            base_url="https://llm.test", api_key="fake-local-key-nothing-real"
        )
        client = self._FakeClient(script)
        provider._client = client  # bypass env lookups; policy under test
        return provider, client, sleeps

    def test_success_after_one_500_retries_with_backoff(self, monkeypatch):
        ok = _ok_sdk_response(valid_content())
        provider, client, sleeps = self._make(
            monkeypatch, [self._sdk_error("server", 500), ok]
        )

        response = provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert response.content == valid_content()
        assert client.calls == 2
        # jitter pinned high: backoff 1s + 0.25s jitter
        assert sleeps == [1.25]

    def test_success_after_two_500s_uses_full_backoff_curve(self, monkeypatch):
        err = self._sdk_error("server", 500)
        ok = _ok_sdk_response(valid_content())
        provider, client, sleeps = self._make(monkeypatch, [err, err, ok])

        provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert client.calls == 3
        assert sleeps == [1.25, 2.25]  # 1s+jitter, 2s+jitter — no third retry

    def test_500_exhausted_maps_to_connection_error(self, monkeypatch):
        err = self._sdk_error("server", 500)
        provider, client, sleeps = self._make(monkeypatch, [err, err, err])

        with pytest.raises(LLMConnectionError) as exc_info:
            provider.complete(
                model="m", system_prompt="s", user_content="u", temperature=0.0
            )

        assert client.calls == provider_module.MAX_ATTEMPTS == 3
        assert len(sleeps) == 2  # retries before attempts 2 and 3 only
        assert "attempt(s)" in str(exc_info.value)

    def test_timeout_retries_at_most_once(self, monkeypatch):
        timeout = self._sdk_error("timeout")
        provider, client, sleeps = self._make(monkeypatch, [timeout, timeout])

        with pytest.raises(LLMTimeoutError):
            provider.complete(
                model="m", system_prompt="s", user_content="u", temperature=0.0
            )

        assert client.calls == 2  # never a third 30s wait inside one handler
        assert sleeps == [1.25]

    def test_timeout_then_success(self, monkeypatch):
        timeout = self._sdk_error("timeout")
        ok = _ok_sdk_response(valid_content())
        provider, client, sleeps = self._make(monkeypatch, [timeout, ok])

        response = provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert response.content == valid_content()
        assert client.calls == 2
        assert sleeps == [1.25]

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_4xx_fails_fast_with_zero_retries(self, monkeypatch, status):
        err = self._sdk_error("status", status_code=status)
        provider, client, sleeps = self._make(monkeypatch, [err])

        with pytest.raises(LLMClientError) as exc_info:
            provider.complete(
                model="m", system_prompt="s", user_content="u", temperature=0.0
            )

        assert client.calls == 1  # fail fast on the first attempt
        assert sleeps == []
        assert exc_info.value.status_code == status
        assert "check LLM_MODEL or key" in str(exc_info.value)

    def test_429_with_numeric_retry_after_is_obeyed(self, monkeypatch):
        rate = self._sdk_error("rate", status_code=429, headers={"Retry-After": "7"})
        ok = _ok_sdk_response(valid_content())
        provider, client, sleeps = self._make(monkeypatch, [rate, ok])

        provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert client.calls == 2
        assert sleeps == [7.25]  # Retry-After 7s (+ capped jitter), not 1s

    def test_429_with_http_date_retry_after_is_obeyed(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        in_3_seconds = (datetime.now(timezone.utc) + timedelta(seconds=3)).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        rate = self._sdk_error(
            "rate", status_code=429, headers={"Retry-After": in_3_seconds}
        )
        ok = _ok_sdk_response(valid_content())
        provider, _client, _ = self._make(monkeypatch, [rate, ok])

        provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert provider is not None  # success reached attempt 2

    def test_429_with_malformed_retry_after_falls_back_to_backoff(self, monkeypatch):
        rate = self._sdk_error(
            "rate", status_code=429, headers={"Retry-After": "later-ish"}
        )
        ok = _ok_sdk_response(valid_content())
        provider, client, sleeps = self._make(monkeypatch, [rate, ok])

        provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert client.calls == 2
        assert sleeps == [1.25]  # backoff curve, header treated as absent

    def test_retry_after_is_capped_at_15_seconds(self, monkeypatch):
        rate = self._sdk_error("rate", status_code=429, headers={"Retry-After": "300"})
        ok = _ok_sdk_response(valid_content())
        provider, _client, sleeps = self._make(monkeypatch, [rate, ok])

        provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert sleeps == [15.25]  # 300s request capped at 15s (+ jitter)

    def test_429_exhausted_maps_to_rate_limit_error(self, monkeypatch):
        rate = self._sdk_error("rate", status_code=429, headers={"Retry-After": "1"})
        provider, client, sleeps = self._make(monkeypatch, [rate, rate, rate])

        with pytest.raises(LLMRateLimitError):
            provider.complete(
                model="m", system_prompt="s", user_content="u", temperature=0.0
            )

        assert client.calls == 3
        assert len(sleeps) == 2

    def test_jitter_bounds_never_negative_and_bounded(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr(provider_module, "_sleep", sleeps.append)
        # Natural (unpatched) jitter: variable in [0, 0.25].
        provider = OpenAICompatProvider(
            base_url="https://llm.test", api_key="fake-local-key-nothing-real"
        )
        err = self._sdk_error("server", 500)
        provider._client = self._FakeClient(
            [err, err, _ok_sdk_response(valid_content())]
        )

        provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert len(sleeps) == 2
        assert all(1.0 <= s <= 1.25 or 2.0 <= s <= 2.25 for s in sleeps)
        assert all(s >= provider_module._BACKOFF_SECONDS[0] for s in sleeps)

    def test_client_built_with_30s_timeout_and_zero_sdk_retries(self, monkeypatch):
        import openai

        captured = {}

        def fake_openai_client(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace()

        monkeypatch.setattr(openai, "OpenAI", fake_openai_client)
        provider = OpenAICompatProvider(
            base_url="https://llm.test", api_key="fake-local-key-nothing-real"
        )

        client = provider._get_client("https://llm.test", "fake-local-key-nothing-real")

        assert client is not None
        assert captured["timeout"] == 30.0
        assert captured["max_retries"] == 0  # we own retries, SDK must not

    def test_policy_constants_match_documented_policy(self):
        assert provider_module.CLIENT_TIMEOUT_SECONDS == 30.0
        assert provider_module.MAX_ATTEMPTS == 3
        assert provider_module.TIMEOUT_MAX_ATTEMPTS == 2
        assert provider_module._BACKOFF_SECONDS == (1.0, 2.0, 4.0)
        assert provider_module._RETRY_AFTER_CAP_SECONDS == 15.0

    def test_token_usage_flows_through_to_response(self, monkeypatch):
        provider, _client, _ = self._make(
            monkeypatch,
            [_ok_sdk_response(valid_content(), input_tokens=11, output_tokens=22)],
        )

        response = provider.complete(
            model="m", system_prompt="s", user_content="u", temperature=0.0
        )

        assert response.usage.input_tokens == 11
        assert response.usage.output_tokens == 22


class TestPipelineTransportMapping:
    """Typed provider failures map to 503/504 at the pipeline layer."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch, tmp_path):
        cache.clear()
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(
            pipeline, "QUARANTINE_LOG_PATH", tmp_path / "quarantine.jsonl"
        )
        self._quarantine_path = tmp_path / "quarantine.jsonl"
        yield
        cache.clear()

    def _inject_raising(self, monkeypatch, exc):
        class RaisingProvider:
            def complete(self, **kwargs):
                raise exc

        monkeypatch.setattr(pipeline, "_get_provider", lambda: RaisingProvider())

    def test_timeout_maps_to_504(self, monkeypatch):
        self._inject_raising(monkeypatch, LLMTimeoutError("boom"))

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 504
        assert "took too long" in exc_info.value.detail
        assert not self._quarantine_path.exists()  # transport ≠ validation failure

    def test_rate_limit_maps_to_503(self, monkeypatch):
        self._inject_raising(monkeypatch, LLMRateLimitError("boom"))

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 503
        assert "rate limiting" in exc_info.value.detail
        assert not self._quarantine_path.exists()

    def test_4xx_client_error_maps_to_503_naming_cause_without_key(self, monkeypatch):
        self._inject_raising(monkeypatch, LLMClientError("rejected", status_code=401))

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 503
        assert "check LLM_MODEL or key" in exc_info.value.detail
        assert "fake" not in exc_info.value.detail  # no key material, ever
        assert not self._quarantine_path.exists()

    def test_connection_error_maps_to_503(self, monkeypatch):
        self._inject_raising(monkeypatch, LLMConnectionError("boom"))

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 503
        assert "unreachable" in exc_info.value.detail
        assert not self._quarantine_path.exists()


class TestPipelineCostLog:
    """Exactly one structured ``llm_call`` line per provider attempt."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch, tmp_path):
        cache.clear()
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_ENABLED", "true")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setattr(
            pipeline, "QUARANTINE_LOG_PATH", tmp_path / "quarantine.jsonl"
        )
        yield
        cache.clear()

    def _llm_call_records(self, caplog) -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "app.services.llm.pipeline"
            and r.getMessage().startswith("llm_call")
        ]

    def test_one_line_per_attempt_with_tokens_and_outcome(self, monkeypatch, caplog):
        bad = json.dumps(valid_payload(category="cooking"))
        good = valid_content()
        usage = LLMUsage(input_tokens=1234, output_tokens=567)
        scripted = ScriptedProvider([bad, good], usage=usage)
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

        with caplog.at_level("INFO", logger="app.services.llm.pipeline"):
            book_classify_service.classify_book("Dune", "Desc.")

        lines = self._llm_call_records(caplog)
        assert len(lines) == 2  # one per provider attempt, no more

        first, second = lines
        assert "attempt=initial" in first
        assert "repaired=False" in first
        assert "cache=miss" in first
        assert "outcome=parse-failed" in first
        assert "input_tokens=1234" in first
        assert "output_tokens=567" in first
        assert "duration_ms=" in first
        assert "model=test-model" in first
        assert "prompt_version=book-classify-v1" in first

        assert "attempt=repair" in second
        assert "repaired=True" in second
        assert "outcome=ok" in second
        assert "input_tokens=1234" in second

        # No secrets on the line.
        for line in lines:
            assert "key" not in line.lower().replace("keys", "") or "fake" not in line

    def test_happy_path_is_one_line_with_ok_outcome(self, monkeypatch, caplog):
        scripted = ScriptedProvider(
            [valid_content()], usage=LLMUsage(input_tokens=5, output_tokens=6)
        )
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)

        with caplog.at_level("INFO", logger="app.services.llm.pipeline"):
            book_classify_service.classify_book("Dune", "Desc.")

        lines = self._llm_call_records(caplog)
        assert len(lines) == 1
        assert "outcome=ok" in lines[0]

    def test_cache_hit_logs_nothing(self, monkeypatch, caplog):
        scripted = ScriptedProvider([valid_content()])
        monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)
        book_classify_service.classify_book("Dune", "Desc.")  # fills the cache

        with caplog.at_level("INFO", logger="app.services.llm.pipeline"):
            caplog.clear()  # drop the initial call's line: only the hit matters
            book_classify_service.classify_book("Dune", "Desc.")  # cache hit

        assert self._llm_call_records(caplog) == []  # no model call, no line

    def test_transport_failure_line_has_zero_tokens_and_outcome(
        self, monkeypatch, caplog
    ):
        class RaisingProvider:
            def complete(self, **kwargs):
                raise LLMTimeoutError("boom")

        monkeypatch.setattr(pipeline, "_get_provider", lambda: RaisingProvider())

        with (
            caplog.at_level("INFO", logger="app.services.llm.pipeline"),
            pytest.raises(HTTPException) as exc_info,
        ):
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 504
        lines = self._llm_call_records(caplog)
        assert len(lines) == 1
        assert "outcome=timeout" in lines[0]
        assert "input_tokens=0" in lines[0]
        assert "output_tokens=0" in lines[0]


class TestKillSwitch:
    """``LLM_ENABLED=false`` — deterministic fallback, zero model/cache work."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        cache.clear()
        monkeypatch.delenv("LLM_STUB", raising=False)
        monkeypatch.setenv("LLM_MODEL", "test-model")
        yield
        cache.clear()

    def _inject_never_called(self, monkeypatch):
        provider = CountingProvider(StubProvider())
        monkeypatch.setattr(pipeline, "_get_provider", lambda: provider)
        # Guard: if the provider IS consulted the test fails loudly.
        return provider

    def test_fallback_is_deterministic_and_schema_valid(self, monkeypatch):
        self._inject_never_called(monkeypatch)
        monkeypatch.setenv("LLM_ENABLED", "false")

        first = book_classify_service.classify_book("Dune", "Desc.")
        second = book_classify_service.classify_book("Dune", "Desc.")

        assert first == second
        assert first.category is BookCategory.OTHER
        assert first.confidence == 0.0
        assert first.quality_flags == []
        assert first.summary == "LLM classification unavailable — kill switch engaged."
        assert first.reason == (
            "The LLM feature is disabled (LLM_ENABLED=false); "
            "deterministic fallback returned."
        )

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "False", "OFF"])
    def test_explicit_falsy_values_engage_kill_switch(self, monkeypatch, value):
        self._inject_never_called(monkeypatch)
        monkeypatch.setenv("LLM_ENABLED", value)

        result = book_classify_service.classify_book("Dune", "Desc.")

        assert result.category is BookCategory.OTHER

    def test_zero_provider_calls_and_no_cache_interaction(self, monkeypatch):
        provider = self._inject_never_called(monkeypatch)
        monkeypatch.setenv("LLM_ENABLED", "false")
        key = cache.cache_key("book-classify-v1", "test-model", {"title": "Dune"})
        cache.put(key, valid_content())  # even a populated cache must be untouched

        book_classify_service.classify_book("Dune", "Desc.")

        assert provider.calls == 0
        assert cache.get(key) == valid_content()  # never dropped nor read for use

    def test_stub_beats_kill_switch_dev_override(self, monkeypatch):
        self._inject_never_called(monkeypatch)
        monkeypatch.setenv("LLM_ENABLED", "false")
        monkeypatch.setenv("LLM_STUB", "1")  # dev override checked FIRST

        result = book_classify_service.classify_book("Dune", "Desc.")

        assert result.category is BookCategory.FICTION  # stub shape, not fallback
        assert result.confidence == 0.9

    def test_blank_llm_enabled_is_not_kill_switch(self, monkeypatch):
        self._inject_never_called(monkeypatch)
        monkeypatch.setenv("LLM_ENABLED", "")

        with pytest.raises(HTTPException) as exc_info:
            book_classify_service.classify_book("Dune", "Desc.")

        assert exc_info.value.status_code == 503  # not-configured, not fallback
