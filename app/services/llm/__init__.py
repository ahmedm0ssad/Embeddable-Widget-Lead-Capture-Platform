"""LLM integration package (A17).

Public surface:

- ``load_prompt`` / ``BOOK_CLASSIFY_PROMPT`` — versioned prompt-file loading.
- ``LLMProvider`` / ``StubProvider`` / ``OpenAICompatProvider`` — the provider
  interface and its two implementations (offline stub + OpenAI-compatible).
- ``cache_key`` / ``get`` / ``put`` / ``clear`` — in-process request cache.
- ``run_book_classify`` — the book-classification pipeline (prompt → stub or
  cache or provider → raw model text at Stage 2; parse/validate is Stage 3).
"""

from app.services.llm.cache import cache_key, clear, get, put
from app.services.llm.pipeline import run_book_classify
from app.services.llm.prompt_loader import BOOK_CLASSIFY_PROMPT, load_prompt
from app.services.llm.provider import (
    DEFAULT_LLM_MODEL,
    LLMConfigError,
    LLMProvider,
    LLMResponse,
    LLMUsage,
    OpenAICompatProvider,
    StubProvider,
)

__all__ = [
    "BOOK_CLASSIFY_PROMPT",
    "DEFAULT_LLM_MODEL",
    "LLMConfigError",
    "LLMProvider",
    "LLMResponse",
    "LLMUsage",
    "OpenAICompatProvider",
    "StubProvider",
    "cache_key",
    "clear",
    "get",
    "load_prompt",
    "put",
    "run_book_classify",
]
