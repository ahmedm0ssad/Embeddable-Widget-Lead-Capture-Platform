"""Book-classification LLM pipeline (A17 Stage 4).

Flow, in order:

1. ``LLM_STUB=1`` → deterministic local stub answer (dev override, Stage 1
   behavior unchanged, including ``missing_description`` for None/empty
   descriptions); provider and cache never consulted. Checked FIRST so test
   suites that pin both flags keep working (see precedence below).
2. ``LLM_ENABLED`` explicitly falsy ("false"/"0"/"no"/"off") → the kill
   switch: schema-valid deterministic fallback, immediate, zero provider and
   cache calls — a production emergency valve.
3. ``LLM_ENABLED`` unset/blank (and no stub) → clean 503 ("LLM integration
   not configured") — a blank line must not silently disable the model.
4. Real path (``LLM_ENABLED`` truthy): prompt → cache → provider → Stage 3
   parser (parse → validate → exactly one schema-repair retry → quarantine +
   HTTP 422). Transport retries (provider-level, Stage 4) are a SEPARATE
   concern from schema repair and stay inside the provider.
5. Cache stores RAW validated model text; hits re-run the same parse/validate
   flow with no provider call.

Precedence (documented + README): ``LLM_STUB=1`` (dev override) →
``LLM_ENABLED=false/0/no/off`` (kill switch) → ``LLM_ENABLED=true`` (real
path) → unset/blank (503). Test suites that pin ``LLM_STUB=1`` AND an empty
``LLM_ENABLED`` therefore get stub mode, not the kill switch; the conftest
pin of an empty ``LLM_ENABLED`` stays meaningful (blank = "not configured").

Error mapping (documented choice): the pipeline raises
``fastapi.HTTPException`` — 503 for config/provider problems, 504 for a
provider timeout (transport deadline, after the provider's one allowed
timeout retry), and 422 for an answer that failed schema validation even
after the one repair attempt. The router never needs its own mapping.
Quarantine is ONLY for model answers that failed schema validation —
transport failures never write a quarantine line.

Cost logging: exactly ONE structured ``llm_call`` log line per provider
attempt (initial and repair; cache hits make no model call and log nothing)
— prompt_version, model, tokens, duration_ms, attempt, repaired, cache,
outcome. No secrets on the line.
"""

import json
import logging
import os
import time
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
    LLMClientError,
    LLMConfigError,
    LLMConnectionError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUsage,
    OpenAICompatProvider,
)

logger = logging.getLogger(__name__)

_RAW_UNCONFIGURED = (
    "LLM integration not configured (stub mode off). "
    "Set LLM_ENABLED=true and LLM_BASE_URL/LLM_API_KEY/LLM_MODEL to enable."
)
_PROVIDER_TIMEOUT = "The LLM provider took too long to answer; try again."
_PROVIDER_RATE_LIMITED = "The LLM provider kept rate limiting; try again later."
_PROVIDER_UNREACHABLE = "The LLM provider is unreachable or failing; try again later."
_PROVIDER_CLIENT_ERROR = "LLM provider rejected the request; check LLM_MODEL or key"
_MODEL_FAILED_VALIDATION = (
    "The LLM returned an answer that failed schema validation, even after one "
    "repair attempt."
)

_ENV_TRUE = {"1", "true", "yes", "on"}
_ENV_EXPLICIT_OFF = {"false", "0", "no", "off"}

# Test seam: point the quarantine log at a temp dir by monkeypatching this.
QUARANTINE_LOG_PATH = quarantine.LOG_PATH

_STUB_REASON = "Stub mode: deterministic answer (no model call)."
_STUB_SUMMARY = (
    "A fictional narrative whose protagonist is drawn into a journey that "
    "reshapes how they understand their own life."
)

_KILL_SWITCH_SUMMARY = "LLM classification unavailable — kill switch engaged."
_KILL_SWITCH_REASON = (
    "The LLM feature is disabled (LLM_ENABLED=false); deterministic fallback "
    "returned."
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


def _kill_switch_fallback() -> ClassifyOutput:
    """Deterministic answer when ``LLM_ENABLED=false`` — never touches the model."""
    return ClassifyOutput(
        category=BookCategory.OTHER,
        summary=_KILL_SWITCH_SUMMARY,
        quality_flags=[],
        confidence=0.0,
        reason=_KILL_SWITCH_REASON,
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


def _map_transport_exception(exc: Exception) -> HTTPException:
    """Map typed provider exceptions to clean HTTP errors (never 500)."""
    if isinstance(exc, LLMConfigError):
        return HTTPException(status_code=503, detail=_RAW_UNCONFIGURED)
    if isinstance(exc, LLMTimeoutError):
        return HTTPException(status_code=504, detail=_PROVIDER_TIMEOUT)
    if isinstance(exc, LLMRateLimitError):
        return HTTPException(status_code=503, detail=_PROVIDER_RATE_LIMITED)
    if isinstance(exc, LLMClientError):
        status = getattr(exc, "status_code", 0) or 0
        return HTTPException(
            status_code=503,
            detail=f"{_PROVIDER_CLIENT_ERROR} (HTTP {status})".rstrip(),
        )
    if isinstance(exc, LLMConnectionError):
        return HTTPException(status_code=503, detail=_PROVIDER_UNREACHABLE)

    # Unknown provider/runtime error — still a clean 503, never a 500.
    return HTTPException(
        status_code=503,
        detail=f"LLM provider call failed: {type(exc).__name__}",
    )


def _outcome_for_exception(exc: Exception) -> str:
    """Outcome label for the ``llm_call`` cost line of a failed transport."""
    if isinstance(exc, LLMTimeoutError):
        return "timeout"
    if isinstance(exc, LLMRateLimitError):
        return "rate-limited"
    if isinstance(exc, LLMClientError):
        return "client-error"
    if isinstance(exc, LLMConnectionError):
        return "connection-failed"
    if isinstance(exc, LLMProviderError):
        return "transport-failed"
    return f"transport-failed:{type(exc).__name__}"


def _log_llm_call(
    *,
    prompt_version: str,
    model: str,
    usage: LLMUsage,
    duration_ms: float,
    attempt: str,
    repaired: bool,
    cache_state: str,
    outcome: str,
) -> None:
    """ONE structured cost line per provider attempt (no secrets on the line)."""
    logger.info(
        "llm_call prompt_version=%s model=%s input_tokens=%d output_tokens=%d "
        "duration_ms=%d attempt=%s repaired=%s cache=%s outcome=%s",
        prompt_version,
        model,
        usage.input_tokens,
        usage.output_tokens,
        int(duration_ms),
        attempt,
        repaired,
        cache_state,
        outcome,
    )


def _run_attempt(
    provider: LLMProvider,
    *,
    model: str,
    prompt_version: str,
    user_content: str,
    system_prompt: str,
    attempt: str,
    repaired: bool,
) -> tuple[ClassifyOutput | None, list[str], str]:
    """One provider round-trip + parse/validate + exactly ONE ``llm_call`` line.

    Returns ``(output, errors, raw)``. Transport failures are logged (0
    tokens, outcome=...) then re-raised mapped to HTTP. Schema failures are
    NOT quarantined here — quarantine stays for FINAL failures only, and the
    caller owns the repair/422 decision.
    """
    started = time.monotonic()
    try:
        response = provider.complete(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.0,
        )
    except Exception as exc:
        _log_llm_call(
            prompt_version=prompt_version,
            model=model,
            usage=LLMUsage(),
            duration_ms=(time.monotonic() - started) * 1000.0,
            attempt=attempt,
            repaired=repaired,
            cache_state="miss",
            outcome=_outcome_for_exception(exc),
        )
        mapped = _map_transport_exception(exc)
        raise mapped from exc

    duration_ms = (time.monotonic() - started) * 1000.0
    raw = response.content
    output, errors = parse_and_validate(raw)
    if output is not None:
        outcome = "ok"
    elif attempt == "initial":
        outcome = "parse-failed"
    else:
        outcome = "repair-failed"
    _log_llm_call(
        prompt_version=prompt_version,
        model=model,
        usage=response.usage or LLMUsage(),  # defensive: fake providers may omit
        duration_ms=duration_ms,
        attempt=attempt,
        repaired=repaired,
        cache_state="miss",
        outcome=outcome,
    )
    return output, errors, raw


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
    """Classify one book record via stub mode, kill switch, cache, or provider."""
    if os.getenv("LLM_STUB") == "1":
        return _stub_classify(title, description)

    flag = os.getenv("LLM_ENABLED", "").strip().lower()
    if flag in _ENV_EXPLICIT_OFF:
        # Kill switch: production emergency valve. No model, no cache.
        return _kill_switch_fallback()
    if flag not in _ENV_TRUE:
        # Unset/blank is "not configured", NOT the kill switch — an empty env
        # line must never silently disable the model.
        raise HTTPException(status_code=503, detail=_RAW_UNCONFIGURED)

    prompt_version, system_prompt = _load_book_prompt()
    model = os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL
    payload: dict = {"title": title, "description": description}
    key = cache.cache_key(prompt_version, model, payload)

    # ---- Cache hit: same parse/validate flow, no provider call (and no
    # llm_call line — nothing was sent to the model), no new quarantine
    # unless the cached blob genuinely fails now (shouldn't happen — only
    # validated raw text is cached); a poisoned entry is evicted so the next
    # call takes the fresh path.
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
    output, errors, raw = _run_attempt(
        provider,
        model=model,
        prompt_version=prompt_version,
        user_content=user_content,
        system_prompt=system_prompt,
        attempt="initial",
        repaired=False,
    )
    if output is not None:
        cache.put(key, raw)
        return output

    # ---- Exactly one schema-repair retry: same system prompt, same original
    # user content plus the rejected answer and the exact rejection reasons.
    # (The provider-level transport retry policy inside complete() is a
    # separate mechanism; this stays a single semantic repair attempt.)
    output, errors, repair_raw = _run_attempt(
        provider,
        model=model,
        prompt_version=prompt_version,
        user_content=_repair_user_content(payload, raw, errors),
        system_prompt=system_prompt,
        attempt="repair",
        repaired=True,
    )
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
