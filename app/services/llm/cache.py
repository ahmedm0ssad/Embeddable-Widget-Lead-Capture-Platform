"""Tiny in-process cache for completed LLM calls.

A plain dict with FIFO trimming at :data:`_MAX_SIZE` (oldest key dropped) —
the simplest correct thing for deduplicating repeated classify calls within
one worker process; the documented limitation is that it is per-process and
lost on restart.

Cache keys MUST include the prompt version: shipping
``book-classify-v2`` would otherwise keep serving stale ``-v1`` answers
(stale-busting).
"""

import hashlib
import json

_MAX_SIZE = 1000

#: Raw model text keyed by :func:`cache_key` digests (module-level on purpose:
#: the pipeline reads/writes it without threading a cache object through).
_CACHE: dict[str, str] = {}


def cache_key(prompt_version: str, model: str, input_payload: dict) -> str:
    """SHA-256 of the canonical payload, salted with prompt version and model."""
    digest = hashlib.sha256()
    digest.update(prompt_version.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(model.encode("utf-8"))
    digest.update(b"\x00")
    payload = json.dumps(input_payload, sort_keys=True, ensure_ascii=False)
    digest.update(payload.encode("utf-8"))
    return digest.hexdigest()


def get(key: str) -> str | None:
    """Return the cached value for ``key`` or ``None`` on miss."""
    return _CACHE.get(key)


def drop(key: str) -> None:
    """Evict one key — used when a cached blob no longer validates."""
    _CACHE.pop(key, None)


def put(key: str, value: str) -> None:
    """Store ``value`` under ``key``, trimming the oldest entry past capacity."""
    if key not in _CACHE and len(_CACHE) >= _MAX_SIZE:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = value


def clear() -> None:
    """Empty the cache (used by tests)."""
    _CACHE.clear()
