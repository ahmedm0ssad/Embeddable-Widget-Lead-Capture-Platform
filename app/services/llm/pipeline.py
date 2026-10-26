"""Book-classification LLM pipeline (A17 Stage 2).

Flow, in order:

1. Load the versioned prompt file — ``(prompt_version, system_prompt)``.
2. ``LLM_STUB=1`` → deterministic local stub answer (Stage 1 behavior,
   unchanged, including ``missing_description`` for None/empty descriptions);
   the provider is never consulted.
3. Real path requires ``LLM_ENABLED`` truthy *and* configured credentials;
   anything less is a clean HTTP 503 ("LLM integration not configured").
4. Cache lookup keyed by ``(prompt_version, model, input payload)`` — a hit
   returns the cached raw text exactly like a fresh answer (the Stage 3
   parser consumes both the same way).
5. User data travels as its own JSON-encoded user message — it is **never**
   concatenated into the system prompt. That is the anti-prompt-injection
   requirement; see the design notes in ``prompts/book-classify-v1.md``.
6. ``provider.complete()`` at temperature 0 → the raw model text is returned
   honestly at Stage 2 as ``{"raw": <text>}``. Parsing, validation and
   repair arrive in Stage 3.

Error mapping (documented choice): the pipeline raises
``fastapi.HTTPException`` directly — 503 for both config problems and
provider/network failures — so a downstream LLM problem can never surface as
a 500 from the endpoint. Stage 3 may restructure this into structured error
values that the router maps instead.
"""

import json
import os
from functools import lru_cache

from fastapi import HTTPException

from app.models.book_classify import (
    BookCategory,
    ClassifyOutput,
    QualityFlag,
    RawClassifyOutput,
)
from app.services.llm import cache
from app.services.llm.prompt_loader import BOOK_CLASSIFY_PROMPT, load_prompt
from app.services.llm.provider import (
    DEFAULT_LLM_MODEL,
    LLMConfigError,
    LLMProvider,
    OpenAICompatProvider,
)

_RAW_UNCONFIGURED = (
    "LLM integration not configured (stub mode off). "
    "Set LLM_ENABLED=true and LLM_BASE_URL/LLM_API_KEY/LLM_MODEL to enable."
)
_TRUE_VALUES = {"1", "true", "yes", "on"}

_STUB_REASON = "Stub mode: deterministic answer (no model call)."
_STUB_SUMMARY = (
    "A fictional narrative whose protagonist is drawn into a journey that "
    "reshapes how they understand their own life."
)


def _stub_classify(title: str, description: str | None) -> ClassifyOutput:
    """Stage 1 stub — string-identical behavior, moved here unchanged."""
    # missing_description is deterministic local validation, not model work.
    quality_flags = (
        [] if description and description.strip() else [QualityFlag.MISSING_DESCRIPTION]
    )
    return ClassifyOutput(
        category=BookCategory.FICTION,
        summary=_STUB_SUMMARY,
        quality_flags=quality_flags,
        confidence=0.9,
        reason=_STUB_REASON,
    )


@lru_cache(maxsize=1)
def _load_book_prompt() -> tuple[str, str]:
    """Load (prompt_version, system_prompt) once and reuse it every call."""
    return load_prompt(BOOK_CLASSIFY_PROMPT)


def _get_provider() -> LLMProvider:
    """Provider seam: every real-mode call goes through this function.

    Tests monkeypatch ``app.services.llm.pipeline._get_provider`` to inject a
    StubProvider (or a counting wrapper around one) — that is how the suite
    stays offline while still exercising the real code path.
    """
    return OpenAICompatProvider()


def run_book_classify(
    title: str, description: str | None
) -> ClassifyOutput | RawClassifyOutput:
    """Classify one book record via stub mode, cache, or the configured provider."""
    if os.getenv("LLM_STUB") == "1":
        return _stub_classify(title, description)

    if os.getenv("LLM_ENABLED", "").strip().lower() not in _TRUE_VALUES:
        raise HTTPException(status_code=503, detail=_RAW_UNCONFIGURED)

    prompt_version, system_prompt = _load_book_prompt()
    model = os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL
    payload: dict = {"title": title, "description": description}
    key = cache.cache_key(prompt_version, model, payload)

    cached = cache.get(key)
    if cached is not None:
        # Cache hit: serve the cached raw text exactly like a fresh answer —
        # the Stage 3 parser consumes both the same way.
        return RawClassifyOutput(raw=cached)

    # Anti-injection: untrusted record data is JSON-encoded into its own user
    # message, never concatenated into the trusted system prompt.
    user_content = json.dumps(payload)

    try:
        response = _get_provider().complete(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.0,
        )
    except LLMConfigError as exc:
        raise HTTPException(status_code=503, detail=_RAW_UNCONFIGURED) from exc
    except Exception as exc:  # provider/network/runtime errors → clean 503, never 500.
        raise HTTPException(
            status_code=503,
            detail=f"LLM provider call failed: {type(exc).__name__}",
        ) from exc

    cache.put(key, response.content)
    return RawClassifyOutput(raw=response.content)
