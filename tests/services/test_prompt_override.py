"""The LLM_PROMPT prompt-file override (A17 stretch: prompt v2 A/B).

Covers the ``prompt_loader.resolve_book_classify_prompt_path`` resolver and
its pipeline integration:

- default (env unset/blank) is always the committed v1 file;
- ``LLM_PROMPT=book-classify-v2.md`` resolves to that file, validated against
  its own ``prompt-version`` marker (stem must equal marker);
- mismatches (marker ≠ stem, bad value shape, unreadable file) raise
  :class:`PromptFormatError` — the override never fails open;
- a v2-prompt run gets a cache namespace disjoint from v1: ``cache_key``
  salts on ``prompt_version``, and the pipeline resolves the file at call
  time, so switching ``LLM_PROMPT`` mid-process never serves stale v1
  answers under the v2 prompt.

All offline: providers are scripted, no network anywhere.
"""

import json

import pytest

from app.services import book_classify_service
from app.services.llm import cache, pipeline
from app.services.llm.prompt_loader import (
    BOOK_CLASSIFY_PROMPT,
    PROMPT_ENV_VAR,
    PROMPTS_DIR,
    PromptFormatError,
    load_prompt,
    resolve_book_classify_prompt_path,
)


def valid_content() -> str:
    return json.dumps(
        {
            "category": "fiction",
            "summary": "A plain summary of a plain book.",
            "quality_flags": [],
            "confidence": 0.9,
            "reason": "It reads as a straightforward narrative.",
        }
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
        from app.services.llm.provider import LLMResponse, LLMUsage

        return LLMResponse(content=self.contents.pop(0), model=model, usage=LLMUsage())


def _real_mode(monkeypatch, contents: int = 1) -> ScriptedProvider:
    """LLM_ENABLED=true real path with the provider injected (offline)."""
    monkeypatch.delenv("LLM_STUB", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    scripted = ScriptedProvider([valid_content()] * contents)
    monkeypatch.setattr(pipeline, "_get_provider", lambda: scripted)
    return scripted


class TestResolveDefaults:
    def test_env_unset_defaults_to_v1_file(self, monkeypatch):
        monkeypatch.delenv(PROMPT_ENV_VAR, raising=False)

        assert resolve_book_classify_prompt_path() == BOOK_CLASSIFY_PROMPT
        version, system_prompt = load_prompt(resolve_book_classify_prompt_path())

        assert version == "book-classify-v1"
        assert system_prompt

    def test_env_blank_defaults_to_v1_file(self, monkeypatch):
        monkeypatch.setenv(PROMPT_ENV_VAR, "   ")

        assert resolve_book_classify_prompt_path() == BOOK_CLASSIFY_PROMPT


class TestResolveOverride:
    def test_env_override_loads_v2_file_with_matching_marker(self, monkeypatch):
        monkeypatch.setenv(PROMPT_ENV_VAR, "book-classify-v2.md")
        path = resolve_book_classify_prompt_path()

        assert path.name == "book-classify-v2.md"
        assert path.parent == PROMPTS_DIR
        version, system_prompt = load_prompt(path)

        # The resolver validated this equality already; re-read to pin it.
        assert version == "book-classify-v2"
        assert "Rose Thistlewood" in system_prompt  # the v2-only example

    def test_marker_not_matching_stem_raises(self, monkeypatch, tmp_path):
        mismatched = tmp_path / "wrong-marker-v3.md"  # stem says v3, marker says v9
        mismatched.write_text(
            "<!-- prompt-version: wrong-marker-v9 -->\n"
            "<!---- SYSTEM-PROMPT-START ---->\n"
            "body\n"
            "<!---- SYSTEM-PROMPT-END ---->\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("app.services.llm.prompt_loader.PROMPTS_DIR", tmp_path)
        monkeypatch.setenv(PROMPT_ENV_VAR, "wrong-marker-v3.md")

        with pytest.raises(PromptFormatError, match="wrong-marker-v3"):
            resolve_book_classify_prompt_path()

    @pytest.mark.parametrize(
        "value",
        [
            "../.env",  # traversal
            "sub/dir/thing.md",  # subdirectory
            "book-classify-v2.txt",  # wrong suffix
            "book-classify v2.md",  # spaces
        ],
    )
    def test_bad_value_shapes_raise(self, monkeypatch, value):
        monkeypatch.setenv(PROMPT_ENV_VAR, value)

        with pytest.raises(PromptFormatError, match="bare '<name>.md'"):
            resolve_book_classify_prompt_path()

    def test_named_but_missing_file_raises(self, monkeypatch):
        monkeypatch.setenv(PROMPT_ENV_VAR, "book-classify-v999.md")

        with pytest.raises(PromptFormatError, match="unreadable"):
            resolve_book_classify_prompt_path()


class TestPipelineOverrideAndCacheNamespace:
    """The pipeline resolves the file at call time; keys salt on version."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        cache.clear()
        pipeline._load_book_prompt.cache_clear()
        pipeline._system_prompt_with_sentinel.cache_clear()
        monkeypatch.delenv(PROMPT_ENV_VAR, raising=False)
        monkeypatch.setattr(
            pipeline, "QUARANTINE_LOG_PATH", tmp_path / "quarantine.jsonl"
        )
        yield
        cache.clear()
        pipeline._load_book_prompt.cache_clear()
        pipeline._system_prompt_with_sentinel.cache_clear()

    def test_default_run_caches_under_v1_key(self, monkeypatch):
        scripted = _real_mode(monkeypatch)
        payload = {"title": "Dune", "description": "A description."}

        book_classify_service.classify_book(**payload)

        assert scripted.system_prompts[0].endswith(pipeline.SENTINEL_INSTRUCTION)
        assert "Rose Thistlewood" not in scripted.system_prompts[0]
        assert list(cache._CACHE) == [
            cache.cache_key("book-classify-v1", "test-model", payload)
        ]

    def test_v2_run_gets_disjoint_cache_namespace(self, monkeypatch):
        monkeypatch.setenv(PROMPT_ENV_VAR, "book-classify-v2.md")
        scripted = _real_mode(monkeypatch)
        payload = {"title": "Dune", "description": "A description."}

        result = book_classify_service.classify_book(**payload)

        assert result.category.name == "FICTION"
        # The v2 system prompt actually went out (the v2-only example rides along).
        assert "Rose Thistlewood" in scripted.system_prompts[0]
        assert list(cache._CACHE) == [
            cache.cache_key("book-classify-v2", "test-model", payload)
        ]
        assert next(iter(cache._CACHE)) != cache.cache_key(
            "book-classify-v1", "test-model", payload
        )

    def test_mid_process_switch_moves_cache_and_prompt(self, monkeypatch):
        payload = {"title": "Dune", "description": "A description."}
        scripted = _real_mode(monkeypatch, contents=2)

        book_classify_service.classify_book(**payload)  # v1 arm
        v1_key = cache.cache_key("book-classify-v1", "test-model", payload)
        assert list(cache._CACHE) == [v1_key]

        monkeypatch.setenv(PROMPT_ENV_VAR, "book-classify-v2.md")
        book_classify_service.classify_book(**payload)  # v2 arm, same process

        # Disjoint namespaces: the same payload is cached once per prompt
        # version, and the v2 call was a fresh provider call (cache miss),
        # never a reuse of the v1 entry.
        v2_key = cache.cache_key("book-classify-v2", "test-model", payload)
        assert v1_key != v2_key
        assert set(cache._CACHE) == {v1_key, v2_key}
        assert "Rose Thistlewood" in scripted.system_prompts[1]
        assert "Rose Thistlewood" not in scripted.system_prompts[0]
        assert scripted.calls == 2  # same payload, fresh version ⇒ no stale cache
