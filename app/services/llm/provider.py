"""LLM provider interface and implementations (the "provider behind an
interface" seam).

Interface contract — providers implement::

    class LLMProvider(Protocol):
        def complete(
            self, *, model: str, system_prompt: str, user_content: str,
            temperature: float,
        ) -> LLMResponse: ...

Stage 2 keeps the return shape deliberately simple: a single
:class:`LLMResponse` holding the raw completion text. ``LLM`` usage capture
(:class:`LLMUsage`) is defined here so the public surface exists, but wiring
usage into responses is Stage 3/4 work.

Implementations:

- :class:`StubProvider` — deterministic canned JSON body, no network, no
  prompt dependence; tests wrap it in counting providers to observe calls.
- :class:`OpenAICompatProvider` — any OpenAI-compatible chat-completions
  endpoint (Groq, OpenAI, Ollama, ...). The ``openai`` package is imported
  lazily inside the client getter — never at module import — so offline tests
  can import and construct this class freely. Credentials are resolved at
  call time (explicit argument first, then ``LLM_BASE_URL``/``LLM_API_KEY``
  env), never at import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_LLM_MODEL = "openai/gpt-oss-20b"


@dataclass
class LLMResponse:
    """Raw completion returned by a provider (unparsed at Stage 2)."""

    content: str
    model: str | None = None


@dataclass
class LLMUsage:
    """Token usage. 0/0 when the provider does not report it."""

    input_tokens: int = 0
    output_tokens: int = 0


class LLMConfigError(RuntimeError):
    """The provider is not usable: required env config is missing."""


class LLMProvider(Protocol):
    """Interface every LLM backend satisfies; the pipeline depends on this."""

    def complete(
        self, *, model: str, system_prompt: str, user_content: str, temperature: float
    ) -> LLMResponse: ...


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


class OpenAICompatProvider:
    """Provider backing onto an OpenAI-compatible ``chat.completions`` API."""

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
        # TODO(a17-stage4): timeouts/retries land with the production client —
        # construct OpenAI(base_url=..., api_key=..., timeout=<s>, max_retries=<n>)
        # (or pass them per call) and add a circulating cost/usage budget; the
        # client above intentionally keeps library defaults for Stage 2.
        response = client.chat.completions.create(
            model=use_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
        )
        content = response.choices[0].message.content or ""
        return LLMResponse(content=content, model=use_model)

    def _get_client(self, base_url: str, api_key: str) -> Any:
        """Build the SDK client lazily and reuse it across calls."""
        if self._client is None:
            import openai  # Lazy so importing this module stays offline-safe.

            self._client = openai.OpenAI(base_url=base_url, api_key=api_key)
        return self._client
