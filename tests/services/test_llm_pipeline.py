"""Offline tests for the A17 LLM package (Stage 3: trustworthy output).

Covers prompt loading from the real committed prompt file, the in-process
cache (stale-busting via prompt version, clear/drop), the provider interface
(stub determinism, OpenAICompat config errors — constructed but never called
with network), the raw-text parser, schema validation (collect ALL errors,
deterministic flag order), and the pipeline: parse → validate → exactly one
repair retry → quarantine + 422, plus cache-hit parsing. No test imports
``openai`` or touches the network.
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
from app.services.llm.parser import (
    RawParseError,
    parse_and_validate,
    parse_json_object,
    validate_classify_payload,
)
from app.services.llm.prompt_loader import PromptFormatError, load_prompt
from app.services.llm.provider import (
    DEFAULT_LLM_MODEL,
    LLMConfigError,
    LLMResponse,
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

    def __init__(self, contents: list[str]):
        self.contents = list(contents)
        self.calls = 0
        self.user_contents: list[str] = []
        self.system_prompts: list[str] = []

    def complete(self, *, model, system_prompt, user_content, temperature):
        self.calls += 1
        self.user_contents.append(user_content)
        self.system_prompts.append(system_prompt)
        return LLMResponse(content=self.contents.pop(0), model=model)


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

    def test_llm_enabled_off_returns_503(self, monkeypatch):
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
