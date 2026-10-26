"""Book-classification LLM pipeline (A17 Stage 3).

Flow, in order:

1. Load the versioned prompt file — ``(prompt_version, system_prompt)``.
2. ``LLM_STUB=1`` → deterministic local stub answer (Stage 1 behavior,
   unchanged, including ``missing_description`` for None/empty descriptions);
   the provider is never consulted.
3. Real path requires ``LLM_ENABLED`` truthy *and* configured credentials;
   anything less is a clean HTTP 503 ("LLM integration not configured").
4. Cache lookup keyed by ``(prompt_version, model, input payload)``. The
   cache stores RAW model text (never parsed output), so the parse/validate
   trail still runs on every hit; only raw text that validated at least once
   is cached, so a validated answer is never lost to a permanent bad entry.
5. User data travels as its own JSON-encoded user message — never
   concatenated into the system prompt (anti-prompt-injection; see the
   design notes in ``prompts/book-classify-v1.md``).
6. ``provider.complete()`` at temperature 0 → raw text goes through the
   Stage 3 parser (make the output trustworthy): parse → validate → exactly
   ONE repair retry (original user content + previous answer + exact
   validation errors) → quarantine + HTTP 422 if it still fails.

Error mapping (documented choice, unchanged 503 side from Stage 2): the
pipeline raises ``fastapi.HTTPException`` — 503 for config/provider problems
(a model call that never happened is "service unavailable", not "bad
answer"), 422 for an answer that failed schema validation even after the one
repair attempt. The router never needs its own mapping. Quarantine failures
are logged before the 422 is raised; quarantine writes themselves can never
crash the pipeline (see ``app.services.llm.quarantine``).
"""

import json
import os
from functools import lru_cache

from fastapi import HTTPException

from app.models.book_classify import (
    BookCategory,
    ClassifyOutput,
    QualityFlag,
)
from app.services.llm import cache, quarantine
from app.services.llm.parser import parse_and_validate
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
_MODEL_FAILED_VALIDATION = (
    "The LLM returned an answer that failed schema validation, even after one "
    "repair attempt."
)
_TRUE_VALUES = {"1", "true", "yes", "on"}

# Test seam: point the quarantine log at a temp dir by monkeypatching this.
QUARANTINE_LOG_PATH = quarantine.LOG_PATH

_STUB_REASON = "Stub mode: deterministic answer (no model call)."
_STUB_SUMMARY = (
    "A fictional narrative whose protagonist is drawn into a journey that "
    "reshapes how they understand their own life."
)

_REPAIR_INSTRUCTION = (
    "Your previous answer was rejected for this reason. Return only corrected "
    "JSON matching the schema. No commentary, no code fences."
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
    fake/scripted provider — that is how the suite stays offline while still
    exercising the full real code path.
    """
    return OpenAICompatProvider()


def _call_provider(
    provider: LLMProvider, *, model: str, user_content: str, system_prompt: str
) -> str:
    """One provider call; maps provider/config failures to a clean 503."""
    try:
        response = provider.complete(
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
    return response.content


def _repair_user_content(payload: dict, raw_output: str, errors: list[str]) -> str:
    """User message for the single repair retry.

    Everything stays JSON-encoded: the original record's user data, the
    rejected answer, and the exact rejection reasons. Keeping the payload a
    JSON *value* (not concatenated prose) preserves the Stage 2
    anti-prompt-injection guarantee — untrusted text remains programmatically
    escaped, never interpolated into instructions.
    """
    return json.dumps(
        {
            "original_record": payload,
            "previous_answer": raw_output,
            "rejection_reasons": errors,
            "instruction": _REPAIR_INSTRUCTION,
        }
    )


def _quarantine(
    *,
    input_payload: dict,
    raw_output: str,
    errors: list[str],
    prompt_version: str,
    model: str,
    attempt: str,
) -> None:
    """Best-effort quarantine line through the module-level test seam."""
    quarantine.log_failure(
        input_payload=input_payload,
        raw_output=raw_output,
        errors=errors,
        prompt_version=prompt_version,
        model=model,
        attempt=attempt,
        path=QUARANTINE_LOG_PATH,
    )


def run_book_classify(title: str, description: str | None) -> ClassifyOutput:
    """Classify one book record via stub mode, cache, or the configured provider."""
    if os.getenv("LLM_STUB") == "1":
        return _stub_classify(title, description)

    if os.getenv("LLM_ENABLED", "").strip().lower() not in _TRUE_VALUES:
        raise HTTPException(status_code=503, detail=_RAW_UNCONFIGURED)

    prompt_version, system_prompt = _load_book_prompt()
    model = os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL
    payload: dict = {"title": title, "description": description}
    key = cache.cache_key(prompt_version, model, payload)

    # ---- Cache hit: same parse/validate flow, no provider call, no new
    # quarantine unless the cached blob genuinely fails now (shouldn't
    # happen — only validated raw text is cached); a poisoned entry is
    # evicted so the next call takes the fresh path.
    cached = cache.get(key)
    if cached is not None:
        output, errors = parse_and_validate(cached)
        if output is not None:
            return output
        _quarantine(
            input_payload=payload,
            raw_output=cached,
            errors=errors,
            prompt_version=prompt_version,
            model=model,
            attempt="cache-hit",
        )
        cache.drop(key)
        raise HTTPException(
            status_code=422,
            detail=_MODEL_FAILED_VALIDATION + " (cache-hit)",
        )

    # ---- Fresh call: user content is a separate JSON-encoded message.
    # Anti-injection: untrusted record data is JSON-encoded into its own user
    # message, never concatenated into the trusted system prompt.
    user_content = json.dumps(payload)
    provider = _get_provider()
    raw = _call_provider(
        provider, model=model, user_content=user_content, system_prompt=system_prompt
    )

    output, errors = parse_and_validate(raw)
    if output is not None:
        cache.put(key, raw)
        return output

    # ---- Exactly one repair retry: same system prompt, same original user
    # content plus the rejected answer and the exact rejection reasons.
    repair_raw = _call_provider(
        provider,
        model=model,
        user_content=_repair_user_content(payload, raw, errors),
        system_prompt=system_prompt,
    )
    output, errors = parse_and_validate(repair_raw)
    if output is not None:
        cache.put(key, repair_raw)
        return output

    # Repair also failed → quarantine the FINAL failure (one line, no line
    # for the recovered initial attempt) and answer with a clean 422.
    _quarantine(
        input_payload=payload,
        raw_output=repair_raw,
        errors=errors,
        prompt_version=prompt_version,
        model=model,
        attempt="repair",
    )
    raise HTTPException(status_code=422, detail=_MODEL_FAILED_VALIDATION)
