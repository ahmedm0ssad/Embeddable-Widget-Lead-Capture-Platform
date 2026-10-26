"""Streaming path for the book-classification pipeline (A17 stretch: SSE).

``run_book_classify_stream`` is the streaming twin of
``app.services.llm.pipeline.run_book_classify``: identical mode precedence and
identical Stage-3 guarantees, with the raw model text arriving as deltas
instead of one blob. It yields :class:`StreamEvent`s for the router's SSE
framing:

- ``token``  — a text delta of the model's raw answer (may occur many times),
- ``result`` — terminal; the *validated* :class:`ClassifyOutput`,
- ``error``  — terminal; ``detail`` mirrors the sync route's HTTPException
  detail verbatim (string for 503/504/422 classes, dict for the pre-send
  token guard 400). In SSE there is no HTTP status to reuse, so the detail
  text IS the status carrier — message wording is shared with sync.

Mode precedence (identical to sync):

1. ``LLM_STUB=1`` → deterministic stub answer, zero provider calls and zero
   cache touches. No ``token`` events are emitted: there is no model text to
   stream, and the validated answer already exists by construction, so the
   stream is a bare terminal ``result`` event (the client contract — "the
   final answer is always the validated schema" — holds without inventing
   raw text that never came from a model).
2. ``LLM_ENABLED`` explicitly falsy ("false"/"0"/"no"/"off") → kill switch:
   deterministic fallback ``result`` event, zero provider and cache calls.
   Stub wins, checked first, exactly like sync.
3. ``LLM_ENABLED`` unset/blank → terminal ``error`` event with the sync 503
   detail ("LLM integration not configured...").
4. Real path: token guard → cache → streamed provider call.

Stage-3 guarantees at the TERMINAL event:

- Cache first: a hit runs the same parse/validate (no provider call, no
  ``llm_call`` line) and emits the ``result`` event immediately — no tokens.
- Deltas are accumulated into a raw buffer; on stream end the SAME
  parse→validate runs. Success → cache the validated raw text + ``result``.
- Failure → the standard ONE schema repair, run as a NON-streamed
  ``complete`` attempt (explicitly allowed for the repair): it reuses the
  sync pipeline's ``_run_attempt`` so the ``llm_call`` cost line, transport
  mapping and retry policy are literally the same code.
- Repair failure → ONE quarantine line (attempt="repair") + terminal
  ``error`` event whose detail is the sync 422 detail string.
- Transport failure during the initial stream (initial connection with its
  retries, or a mid-stream break that is never retried — see
  ``provider.complete_stream``) → ONE ``llm_call`` line with the matching
  outcome label + terminal ``error`` event; NO repair, NO quarantine,
  because this is not a schema failure.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass

from app.models.book_classify import ClassifyOutput
from app.services.llm import cache
from app.services.llm import pipeline as sync_flow
from app.services.llm.parser import parse_and_validate

# Test seam parity note: provider injection stays at ONE place —
# ``app.services.llm.pipeline._get_provider`` — because this module calls it
# through the pipeline module object, so the same monkeypatch drives both the
# sync and streaming paths.


@dataclass(frozen=True)
class StreamEvent:
    """One typed event of the classification stream (terminal kinds end it)."""

    kind: str  # "token" | "result" | "error"
    delta: str = ""  # kind == "token"
    output: ClassifyOutput | None = None  # kind == "result"
    detail: object = None  # kind == "error" — str or dict (token guard)


def run_book_classify_stream(
    title: str, description: str | None
) -> Iterator[StreamEvent]:
    """Stream-classify one book record; the terminal event is always validated."""

    # ---- 1. Stub mode (checked first: stub beats the kill switch, like sync).
    if os.getenv("LLM_STUB") == "1":
        yield StreamEvent(
            kind="result", output=sync_flow._stub_classify(title, description)
        )
        return

    # ---- 2. Kill switch (explicit off): deterministic fallback, no model.
    flag = os.getenv("LLM_ENABLED", "").strip().lower()
    if flag in sync_flow._ENV_EXPLICIT_OFF:
        yield StreamEvent(kind="result", output=sync_flow._kill_switch_fallback())
        return

    # ---- 3. Unset/blank is "not configured", NOT the kill switch: sync maps
    # this to a clean 503 before any stream; the streaming answer is the same
    # wording as a terminal error event (HTTP is already 200 SSE).
    if flag not in sync_flow._ENV_TRUE:
        yield StreamEvent(kind="error", detail=sync_flow._RAW_UNCONFIGURED)
        return

    # ---- 4. Real path: identical prompt/sentinel/payload/token-guard setup.
    # The optional LLM_PROMPT file override is resolved at call time (default
    # v1) — the same resolution the sync path runs, so both paths always
    # classify with the same prompt.
    prompt_version, system_prompt = sync_flow._system_prompt_with_sentinel(
        str(sync_flow.resolve_book_classify_prompt_path())
    )
    model = os.getenv("LLM_MODEL") or sync_flow.DEFAULT_LLM_MODEL
    payload: dict = {"title": title, "description": description}
    user_content = json.dumps(payload)

    estimated_tokens = sync_flow._estimate_tokens(
        system_prompt
    ) + sync_flow._estimate_tokens(user_content)
    max_tokens = sync_flow._max_request_tokens()
    if estimated_tokens > max_tokens:
        # Account for the would-be spend exactly like sync: one cost line
        # with outcome="rejected-too-long", then the same dict detail.
        sync_flow._log_llm_call(
            prompt_version=prompt_version,
            model=model,
            usage=sync_flow.LLMUsage(input_tokens=estimated_tokens),
            duration_ms=0.0,
            attempt="initial",
            repaired=False,
            cache_state="miss",
            outcome="rejected-too-long",
        )
        yield StreamEvent(
            kind="error",
            detail={
                "error": sync_flow._REQUEST_TOO_LONG_ERROR,
                "message": sync_flow._REQUEST_TOO_LONG,
                "estimated_tokens": estimated_tokens,
                "limit": max_tokens,
            },
        )
        return

    # ---- Cache first: hit → terminal result with no stream and no model call.
    key = cache.cache_key(prompt_version, model, payload)
    cached = cache.get(key)
    if cached is not None:
        output, errors = parse_and_validate(cached)
        if output is not None:
            yield StreamEvent(kind="result", output=output)
            return
        # Poisoned entry: same quarantine + evict + "(cache-hit)" wording.
        sync_flow._quarantine(
            input_payload=payload,
            raw_output=cached,
            errors=errors,
            prompt_version=prompt_version,
            model=model,
            attempt="cache-hit",
        )
        cache.drop(key)
        yield StreamEvent(
            kind="error", detail=sync_flow._MODEL_FAILED_VALIDATION + " (cache-hit)"
        )
        return

    # ---- Fresh call: stream the initial attempt, accumulating raw deltas.
    provider = sync_flow._get_provider()
    buffer: list[str] = []
    started = time.monotonic()
    try:
        for delta in _stream_provider(
            provider,
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
        ):
            buffer.append(delta)
            yield StreamEvent(kind="token", delta=delta)
    except Exception as exc:  # mapped below — transport failures are clean
        sync_flow._log_llm_call(
            prompt_version=prompt_version,
            model=model,
            usage=sync_flow.LLMUsage(),
            duration_ms=(time.monotonic() - started) * 1000.0,
            attempt="initial",
            repaired=False,
            cache_state="miss",
            outcome=sync_flow._outcome_for_exception(exc),
        )
        mapped = sync_flow._map_transport_exception(exc)
        yield StreamEvent(kind="error", detail=mapped.detail)
        return

    raw = "".join(buffer)
    duration_ms = (time.monotonic() - started) * 1000.0
    output, errors = parse_and_validate(raw)
    sync_flow._log_llm_call(
        prompt_version=prompt_version,
        model=model,
        usage=sync_flow.LLMUsage(),
        duration_ms=duration_ms,
        attempt="initial",
        repaired=False,
        cache_state="miss",
        outcome="ok" if output is not None else "parse-failed",
    )
    if output is not None:
        # Only validated raw text is cached — the SAME rule as sync.
        cache.put(key, raw)
        yield StreamEvent(kind="result", output=output)
        return

    # ---- Exactly one schema repair — a NON-streamed attempt is allowed (and
    # used): _run_attempt reuses sync's cost line + transport mapping so the
    # repair semantics are byte-identical to the sync route.
    try:
        output, errors, repair_raw = sync_flow._run_attempt(
            provider,
            model=model,
            prompt_version=prompt_version,
            user_content=sync_flow._repair_user_content(payload, raw, errors),
            system_prompt=system_prompt,
            attempt="repair",
            repaired=True,
        )
    except Exception as exc:
        # _run_attempt already logged its own cost line; here we only
        # surface the SAME mapped detail in-stream.
        yield StreamEvent(kind="error", detail=_mapped_detail(exc))
        return

    if output is not None:
        cache.put(key, repair_raw)
        yield StreamEvent(kind="result", output=output)
        return

    # ---- Repair also failed: ONE quarantine line + the sync 422 wording.
    sync_flow._quarantine(
        input_payload=payload,
        raw_output=repair_raw,
        errors=errors,
        prompt_version=prompt_version,
        model=model,
        attempt="repair",
    )
    yield StreamEvent(kind="error", detail=sync_flow._MODEL_FAILED_VALIDATION)


def _stream_provider(
    provider, *, model: str, system_prompt: str, user_content: str
) -> Iterator[str]:
    """Yield the model's raw text as deltas for one attempt.

    Providers implementing ``complete_stream`` stream for real; providers
    that only implement ``complete`` (e.g. hand-written test fakes) deliver
    their whole answer as ONE synthetic delta so the accumulate/validate
    flow stays identical.
    """
    complete_stream = getattr(provider, "complete_stream", None)
    if callable(complete_stream):
        yield from complete_stream(
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.0,
        )
        return
    response = provider.complete(
        model=model,
        system_prompt=system_prompt,
        user_content=user_content,
        temperature=0.0,
    )
    yield response.content


def _mapped_detail(exc: Exception) -> object:
    """The sync-mapped detail for a transport failure (never a bare 500)."""
    if isinstance(exc, sync_flow.HTTPException):
        return exc.detail
    return sync_flow._map_transport_exception(exc).detail
