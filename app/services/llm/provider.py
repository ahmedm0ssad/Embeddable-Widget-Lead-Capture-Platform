"""LLM provider interface and implementations (the "provider behind an
interface" seam).

Interface contract — providers implement::

    class LLMProvider(Protocol):
        def complete(
            self, *, model: str, system_prompt: str, user_content: str,
            temperature: float,
        ) -> LLMResponse: ...

:class:`LLMResponse` carries token usage (:class:`LLMUsage`, 0/0 when the
provider does not report it — :class:`StubProvider` always reports 0/0), and
the pipeline turns it into the one ``llm_call`` cost-log line per attempt.

Stage 4 production behavior — the transport policy lives entirely inside
``OpenAICompatProvider.complete`` (transport retries are OURS here; the
schema-repair retry in the pipeline is a separate concern and stays exactly
one):

- Client: ``openai.OpenAI(timeout=30.0, max_retries=0)``. The SDK must not
  silently retry (its default ``max_retries=2`` would multiply calls and
  burn quota) — this module owns the retry policy. The client is a lazy
  per-instance singleton: no lock is needed because construction is
  idempotent and providers are per-request/short-lived; a module-level
  singleton would couple unrelated credential sets.
- Timeout: 30.0s (explicitly ≤ the 60s bound).
- Retried (transport-level): ``APITimeoutError`` / ``APIConnectionError``,
  429 ``RateLimitError``, and 5xx ``InternalServerError``/``APIStatusError``.
  Budget: 3 attempts total (2 retries) with exponential backoff 1s → 2s → 4s
  plus ≤ 0.25s random jitter. A 429's ``Retry-After`` header overrides the
  backoff when it is numeric seconds or an HTTP-date (both parsed
  defensively; unparsable → fall back to the backoff curve), capped at 15s.
- Timeout special case: ``APITimeoutError`` retries AT MOST ONCE (max 2
  attempts) so one HTTP handler can never spend 3 × 30s inside the client
  timeout; worst case ≈ 2 × 30s + ≤ 1.25s sleep. Every other failure is fast
  (3 attempts ≈ failures + ≤ 3.25s sleeps).
- NEVER retried: 400/401/403/404 and any other 4xx — fail fast on the first
  attempt with ZERO retries/sleeps, surfaced as :class:`LLMClientError`.
- Final failures surface as typed exceptions the pipeline maps to HTTP:
  :class:`LLMTimeoutError` → 504, everything else → 503.

The ``openai`` package is imported lazily (inside ``_get_client`` or the
exception-of-record paths) — never at module import — so offline tests can
import and construct this class freely. Credentials are resolved at call
time (explicit argument first, then ``LLM_BASE_URL``/``LLM_API_KEY`` env),
never at import. Sleep is injectable: tests patch the module-level
``_sleep``.

Streaming (A17 stretch): ``OpenAICompatProvider.complete_stream`` issues the
same ``chat.completions`` request with ``stream=True`` and yields the text
deltas (``choice.delta.content``). The transport retry policy applies to the
INITIAL connection only — the retry loop lives around "create the stream and
read the first content delta". Once deltas have flowed we are COMMITTED to
this response: a stream that breaks after 200 OK is never retried (replaying
the request could double-bill a completion whose prefix was already emitted
to the client); the mid-stream failure surfaces as ONE typed
:class:`LLMProviderError` that the streaming pipeline turns into a terminal
SSE ``error`` event. :class:`StubProvider` gains a deterministic fake stream
so offline tests exercise the identical accumulate/validate flow.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any, Protocol

DEFAULT_LLM_MODEL = "openai/gpt-oss-20b"

#: Opt-in structured-output switch (stretch 5): when this env var is set to
#: ``json_schema`` (read at call time, like the credentials), ``complete``
#: adds ``response_format={"type": "json_schema", ...}`` compiled from
#: :class:`~app.models.book_classify.ClassifyOutput`. Default OFF: on models
#: that support it, malformed JSON becomes essentially impossible; on models
#: that do not, the request is a 4xx error — which is exactly why it is
#: opt-in per deployment rather than a hard-coded capability.
_RESPONSE_FORMAT_ENV = "LLM_RESPONSE_FORMAT"
_RESPONSE_FORMAT_NAME = "classify_output"

#: Client-side read timeout (seconds) — well under the 60s handler bound.
CLIENT_TIMEOUT_SECONDS = 30.0

#: Transport retry policy constants (documented above; tests assert them).
MAX_ATTEMPTS = 3
#: Timeouts get at most one retry so one handler ≤ ~60s despite a 30s timeout.
TIMEOUT_MAX_ATTEMPTS = 2
_BACKOFF_SECONDS = (1.0, 2.0, 4.0)  # sleep before retry 1, 2, 3
_JITTER_SECONDS = 0.25
_RETRY_AFTER_CAP_SECONDS = 15.0

_sleep = time.sleep  # Test seam: patch app.services.llm.provider._sleep.


@dataclass
class LLMUsage:
    """Token usage. 0/0 when the provider does not report it."""

    input_tokens: int = 0
    output_tokens: int = 0


def _strict_json_schema(node: Any) -> Any:
    """Provider-tolerant schema: drop ``title`` cosmetics, close every object.

    Groq's strict json_schema mode rejects any object without
    ``additionalProperties: false``; pydantic's output leaves that key off.
    Applied recursively to every object node (root + ``$defs``).
    """
    if isinstance(node, dict):
        fixed = {k: _strict_json_schema(v) for k, v in node.items() if k != "title"}
        if "properties" in fixed or fixed.get("type") == "object":
            fixed.setdefault("additionalProperties", False)
        return fixed
    if isinstance(node, list):
        return [_strict_json_schema(v) for v in node]
    return node


@lru_cache(maxsize=1)
def _classify_json_schema() -> dict[str, Any]:
    """JSON Schema compiled from ``ClassifyOutput`` (stripped + strict, cached)."""
    from app.models.book_classify import ClassifyOutput  # offline-safe lazy import

    return _strict_json_schema(ClassifyOutput.model_json_schema())


def _response_format() -> dict[str, Any] | None:
    """Structured-output body for an opt-in ``LLM_RESPONSE_FORMAT=json_schema``.

    Absent/unset/any other value → ``None`` (provider sends no field).
    """
    if os.getenv(_RESPONSE_FORMAT_ENV, "").strip().lower() != "json_schema":
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _RESPONSE_FORMAT_NAME,
            "schema": _classify_json_schema(),
            "strict": True,
        },
    }


@dataclass
class LLMResponse:
    """Raw completion returned by a provider, plus token usage."""

    content: str
    model: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)


class LLMConfigError(RuntimeError):
    """The provider is not usable: required env config is missing."""


class LLMProviderError(Exception):
    """Base for provider call failures after the internal retry policy."""


class LLMTimeoutError(LLMProviderError):
    """Transport deadline exceeded, even after the one allowed timeout retry."""


class LLMConnectionError(LLMProviderError):
    """Connection or ≥500 failure, still failing across the retry budget."""


class LLMRateLimitError(LLMProviderError):
    """The provider kept answering 429 across the retry budget."""


class LLMClientError(LLMProviderError):
    """The provider rejected the request (any 4xx). Never retried, fail fast."""

    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class LLMProvider(Protocol):
    """Interface every LLM backend satisfies; the pipeline depends on this."""

    def complete(
        self, *, model: str, system_prompt: str, user_content: str, temperature: float
    ) -> LLMResponse: ...


class LLMStreamingProvider(Protocol):
    """Optional streaming extension of :class:`LLMProvider`.

    Providers that support incremental output implement this in addition to
    ``complete``; the sync pipeline only ever requires ``complete``, while the
    streaming pipeline prefers ``complete_stream`` (checked with
    ``getattr``) and falls back to a one-shot ``complete`` delivery when a
    provider (e.g. a test fake) does not implement it.
    """

    def complete_stream(
        self, *, model: str, system_prompt: str, user_content: str, temperature: float
    ) -> Iterator[str]: ...


def _delta_text(chunk: Any) -> str | None:
    """Extract ``choices[0].delta.content`` from one streamed chunk.

    Returns ``None`` for chunks with no choices or no content (role-only
    keep-alives, usage-only final chunks). Tolerates dict-shaped choices so
    hand-built fakes work the same way as SDK objects.
    """
    choices = getattr(chunk, "choices", None) or ()  # defensive: chunk lied
    if not choices:
        return None
    choice = choices[0]
    delta = getattr(choice, "delta", None)
    if delta is None and isinstance(choice, dict):
        delta = choice.get("delta")
    if delta is None:
        return None
    content = getattr(delta, "content", None)
    if content is None and isinstance(delta, dict):
        content = delta.get("content")
    return content or None  # empty-string content counts as absent


def _mid_stream_error(exc: BaseException) -> LLMProviderError:
    """Typed error for a stream that broke AFTER deltas started flowing.

    By policy this is NEVER retried (see the module docstring); the mapping
    keeps transport shapes recognizable so the pipeline renders the same
    client-facing wording as the sync path (503 unhappy classes, or the
    caller's own typed injection passing through unchanged).
    """
    if isinstance(exc, LLMProviderError):
        return exc  # already typed — the pipeline maps it as-is
    return LLMConnectionError(
        f"LLM stream failed mid-flight after deltas started ({type(exc).__name__})"
    )


_STUB_CONTENT = (
    '{"category": "fiction", '
    '"summary": "A fictional narrative whose protagonist is drawn into a '
    'journey that reshapes how they understand their own life.", '
    '"quality_flags": [], '
    '"confidence": 0.9, '
    '"reason": "Stub provider: deterministic canned answer (no model call)."}'
)


class StubProvider:
    """Deterministic offline provider.

    Returns a compact, schema-correct canned JSON body regardless of the
    model, prompts, or temperature — prompt-free on purpose so it can never
    leak request text into behavior.
    """

    def __init__(self, content: str = _STUB_CONTENT, model: str | None = "stub"):
        self.content = content
        self.model = model

    def complete(
        self, *, model: str, system_prompt: str, user_content: str, temperature: float
    ) -> LLMResponse:
        return LLMResponse(content=self.content, model=self.model)

    def complete_stream(
        self, *, model: str, system_prompt: str, user_content: str, temperature: float
    ) -> Iterator[str]:
        """Fake stream: the canned JSON body in a few deterministic chunks.

        No network, no model: the accumulated buffer is byte-identical to
        :attr:`content`, so the streaming pipeline's parse/validate step
        behaves exactly like the sync stub path. Splitting into ~4 chunks
        exercises the multi-delta accumulation offline.
        """
        step = max(1, len(self.content) // 4)
        for index in range(0, len(self.content), step):
            yield self.content[index : index + step]


def _usage_of_sdk_response(response: Any) -> LLMUsage:
    """Token usage from an SDK response; 0/0 when absent or malformed."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage()
    try:
        return LLMUsage(
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )
    except (TypeError, ValueError):  # defensive: provider lied about shape
        return LLMUsage()


def _retry_after_seconds(exc: Any) -> float | None:
    """Obey ``Retry-After`` from a 429: numeric seconds or HTTP-date.

    Returns None when absent, unparsable, or the exception carries no
    response headers — the caller then falls back to the backoff curve.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1", errors="replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
        if when.tzinfo is None:  # RFC 7231 dates are UTC per spec
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:  # unparsable HTTP-date → treat as absent
        return None


def _next_delay(attempt: int, exc: Any) -> float:
    """Delay before the next transport attempt.

    ``attempt`` is the attempt that just failed (1-based). Retry-After (when
    present on a 429) is honored and capped, then jittered; otherwise the
    exponential backoff curve (1s → 2s → 4s, held at 4s past the table) is
    jittered. Jitter is up to :data:`_JITTER_SECONDS` and never negative.
    """
    index = min(attempt - 1, len(_BACKOFF_SECONDS) - 1)
    base = _BACKOFF_SECONDS[index]
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        base = min(retry_after, _RETRY_AFTER_CAP_SECONDS)
    return base + random.uniform(0.0, _JITTER_SECONDS)


class OpenAICompatProvider:
    """Provider backing onto an OpenAI-compatible ``chat.completions`` API.

    Owns the transport retry policy documented in the module docstring.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._client: Any | None = None

    def complete(
        self, *, model: str, system_prompt: str, user_content: str, temperature: float
    ) -> LLMResponse:
        # Env is read at call time — credentials can appear after import, and
        # an unconfigured instance raises a clear LLMConfigError naming the
        # missing variable instead of failing deep inside the SDK.
        base_url = self.base_url or os.getenv("LLM_BASE_URL")
        api_key = self.api_key or os.getenv("LLM_API_KEY")
        use_model = model or self.model or os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL

        missing = [
            name
            for name, value in (("LLM_BASE_URL", base_url), ("LLM_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise LLMConfigError(
                "LLM provider not configured: missing required env var(s): "
                + ", ".join(missing)
            )

        client = self._get_client(base_url, api_key)
        import openai  # Local: offline tests never import this at module level.

        # Opt-in structured output (stretch 5): env read at call time so the
        # switch needs no restart, exactly like the credentials above.
        extra_kwargs: dict[str, Any] = {}
        response_format = _response_format()
        if response_format is not None:
            extra_kwargs["response_format"] = response_format

        attempt = 0
        while True:
            attempt += 1
            try:
                response = client.chat.completions.create(
                    model=use_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=temperature,
                    **extra_kwargs,
                )
            except openai.APITimeoutError as exc:
                # Timeouts retry at most once (TIMEOUT_MAX_ATTEMPTS): a 30s
                # timeout must never cost 3 × 30s in one HTTP handler.
                if attempt < TIMEOUT_MAX_ATTEMPTS:
                    _sleep(_next_delay(attempt, None))
                    continue
                raise LLMTimeoutError(
                    f"LLM provider did not answer within {CLIENT_TIMEOUT_SECONDS:.0f}s "
                    f"(gave up after {attempt} attempt(s))"
                ) from exc
            except openai.APIConnectionError as exc:
                if attempt < MAX_ATTEMPTS:
                    _sleep(_next_delay(attempt, None))
                    continue
                raise LLMConnectionError(
                    f"LLM provider unreachable after {attempt} attempt(s)"
                ) from exc
            except openai.RateLimitError as exc:
                # Retry-After (when present) caps sleep at 15s instead of the
                # backoff curve, so the retry budget stays bounded.
                if attempt < MAX_ATTEMPTS:
                    _sleep(_next_delay(attempt, exc))
                    continue
                raise LLMRateLimitError(
                    f"LLM provider kept rate limiting after {attempt} attempt(s)"
                ) from exc
            except openai.APIStatusError as exc:
                status = getattr(exc, "status_code", 0) or 0
                if status >= 500:
                    if attempt < MAX_ATTEMPTS:
                        _sleep(_next_delay(attempt, None))
                        continue
                    raise LLMConnectionError(
                        f"LLM provider kept failing (HTTP {status}) after "
                        f"{attempt} attempt(s)"
                    ) from exc
                # Any 4xx is permanent: config/prompt/key problem. Fail fast,
                # zero retries — and never include the key in the message.
                raise LLMClientError(
                    f"LLM provider rejected the request (HTTP {status}); "
                    "check LLM_MODEL or key",
                    status_code=status,
                ) from exc

            content = response.choices[0].message.content or ""
            return LLMResponse(
                content=content, model=use_model, usage=_usage_of_sdk_response(response)
            )

    def complete_stream(
        self, *, model: str, system_prompt: str, user_content: str, temperature: float
    ) -> Iterator[str]:
        """Stream the completion as text deltas (same request shape as ``complete``).

        Retry policy: the FULL transport policy (documented in the module
        docstring) applies to the initial connection — implemented by
        :meth:`_open_stream_with_retry`, which owns "create the stream and
        read the first content delta". Once that returns we are COMMITTED:
        deltas have flowed to the caller, so no mid-stream retry can ever
        happen — a stream that breaks after 200 OK raises exactly one typed
        :class:`LLMProviderError` (converted from whatever the SDK raised)
        instead of silently truncating or replaying the request.

        The openai package is imported lazily like every other path here, so
        offline tests can exercise this code without the SDK installed.
        """
        base_url = self.base_url or os.getenv("LLM_BASE_URL")
        api_key = self.api_key or os.getenv("LLM_API_KEY")
        use_model = model or self.model or os.getenv("LLM_MODEL") or DEFAULT_LLM_MODEL

        missing = [
            name
            for name, value in (("LLM_BASE_URL", base_url), ("LLM_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise LLMConfigError(
                "LLM provider not configured: missing required env var(s): "
                + ", ".join(missing)
            )

        client = self._get_client(base_url, api_key)
        stream, first_delta = self._open_stream_with_retry(
            client,
            model=use_model,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
        )

        # ---- Committed phase: deltas flowed, no retries past this point.
        try:
            if first_delta:
                yield first_delta
            for chunk in stream:
                text = _delta_text(chunk)
                if text:
                    yield text
        except Exception as exc:
            raise _mid_stream_error(exc) from exc

    def _open_stream_with_retry(
        self,
        client: Any,
        *,
        model: str,
        system_prompt: str,
        user_content: str,
        temperature: float,
    ) -> tuple[Any, str]:
        """Create ``stream=True`` request + read the FIRST content delta.

        This is the ONLY retriable phase of :meth:`complete_stream`: the same
        failure classes as ``complete`` retry here (timeout ≤ once, connection
        /429/5xx within the 3-attempt budget, 4xx never). Returns
        ``(stream, first_delta_text)`` — ``first_delta_text`` is ``""`` when
        the stream ended without any content.
        """
        import openai  # Local: offline tests never import this at module level.

        extra_kwargs: dict[str, Any] = {}
        response_format = _response_format()
        if response_format is not None:
            extra_kwargs["response_format"] = response_format

        kwargs: dict[str, Any] = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            stream=True,
            **extra_kwargs,
        )

        attempt = 0
        while True:
            attempt += 1
            try:
                stream = client.chat.completions.create(**kwargs)
                first_delta = ""
                for chunk in stream:
                    text = _delta_text(chunk)
                    if text:
                        first_delta = text
                        break
                return stream, first_delta
            except openai.APITimeoutError as exc:
                # Timeouts retry at most once (TIMEOUT_MAX_ATTEMPTS): a 30s
                # timeout must never cost 3 × 30s in one HTTP handler.
                if attempt < TIMEOUT_MAX_ATTEMPTS:
                    _sleep(_next_delay(attempt, None))
                    continue
                raise LLMTimeoutError(
                    f"LLM provider did not answer within "
                    f"{CLIENT_TIMEOUT_SECONDS:.0f}s (gave up after {attempt} "
                    "attempt(s))"
                ) from exc
            except openai.APIConnectionError as exc:
                if attempt < MAX_ATTEMPTS:
                    _sleep(_next_delay(attempt, None))
                    continue
                raise LLMConnectionError(
                    f"LLM provider unreachable after {attempt} attempt(s)"
                ) from exc
            except openai.RateLimitError as exc:
                if attempt < MAX_ATTEMPTS:
                    _sleep(_next_delay(attempt, exc))
                    continue
                raise LLMRateLimitError(
                    f"LLM provider kept rate limiting after {attempt} attempt(s)"
                ) from exc
            except openai.APIStatusError as exc:
                status = getattr(exc, "status_code", 0) or 0
                if status >= 500:
                    if attempt < MAX_ATTEMPTS:
                        _sleep(_next_delay(attempt, None))
                        continue
                    raise LLMConnectionError(
                        f"LLM provider kept failing (HTTP {status}) after "
                        f"{attempt} attempt(s)"
                    ) from exc
                # Any 4xx is permanent: config/prompt/key problem. Fail fast,
                # zero retries — and never include the key in the message.
                raise LLMClientError(
                    f"LLM provider rejected the request (HTTP {status}); "
                    "check LLM_MODEL or key",
                    status_code=status,
                ) from exc

    def _get_client(self, base_url: str, api_key: str) -> Any:
        """Build the SDK client lazily (per instance) and reuse it."""
        if self._client is None:
            import openai  # Lazy so importing this module stays offline-safe.

            # max_retries=0: retry policy is owned above; the SDK default
            # (2 retries) would multiply calls and burn quota silently.
            self._client = openai.OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=CLIENT_TIMEOUT_SECONDS,
                max_retries=0,
            )
        return self._client
