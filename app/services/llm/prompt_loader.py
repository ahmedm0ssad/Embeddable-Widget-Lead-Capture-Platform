"""Versioned prompt-file loading.

Prompt files are code: they carry a version marker and a machine-consumed
system-prompt block, and both are mandatory. A file that is missing either is
treated as a broken build and fails loudly, rather than silently sending a
truncated or wrong prompt to the model.

Layout of a prompt file (see ``prompts/book-classify-v1.md``):

    <!-- prompt-version: book-classify-v1 -->
    <!---- SYSTEM-PROMPT-START ---->
    ...the exact text that becomes the system message...
    <!---- SYSTEM-PROMPT-END ---->
    ...design notes (never sent to the model)...
"""

import os
import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
BOOK_CLASSIFY_PROMPT = PROMPTS_DIR / "book-classify-v1.md"

#: Optional file override read at call time (never at import): setting
#: ``LLM_PROMPT=book-classify-v2.md`` makes the pipeline classify with the
#: v2 prompt file instead of the v1 default. Every default path stays v1.
PROMPT_ENV_VAR = "LLM_PROMPT"

_VERSION_PATTERN = re.compile(r"<!--\s*prompt-version:\s*(\S+)\s*-->")
_SYSTEM_PROMPT_START = "<!---- SYSTEM-PROMPT-START ---->"
_SYSTEM_PROMPT_END = "<!---- SYSTEM-PROMPT-END ---->"

# An override must name exactly "<name>.md" — a bare file name, no directories,
# no traversal, no other extension. Validated against PROMPTS_DIR only.
_PROMPT_ENV_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.md$")


class PromptFormatError(ValueError):
    """A prompt file is missing its version marker or system-prompt markers."""


def load_prompt(path: Path) -> tuple[str, str]:
    """Read a prompt markdown file and return ``(prompt_version, system_prompt)``.

    The version comes from the ``<!-- prompt-version: vX -->`` marker; the
    system prompt is exactly the text between the SYSTEM-PROMPT-START/END
    markers (stripped of surrounding blank lines). Design notes below the end
    marker are ignored. Raises :class:`PromptFormatError` on missing markers,
    empty system prompt, split marker pairs, or an unreadable file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptFormatError(f"Prompt file unreadable: {path} ({exc})") from exc

    version_match = _VERSION_PATTERN.search(text)
    if version_match is None:
        raise PromptFormatError(
            f"Prompt file is missing a 'prompt-version' marker: {path}"
        )

    start = text.find(_SYSTEM_PROMPT_START)
    if start == -1:
        raise PromptFormatError(
            f"Prompt file is missing the SYSTEM-PROMPT-START marker: {path}"
        )
    start += len(_SYSTEM_PROMPT_START)

    end = text.find(_SYSTEM_PROMPT_END, start)
    if end == -1:
        raise PromptFormatError(
            f"Prompt file is missing the SYSTEM-PROMPT-END marker: {path}"
        )

    system_prompt = text[start:end].strip()
    if not system_prompt:
        raise PromptFormatError(f"Prompt file has an empty system prompt: {path}")

    return version_match.group(1), system_prompt


def resolve_book_classify_prompt_path() -> Path:
    """Resolve which prompt file the book-classify pipeline should load.

    Read at call time so a single process can honor an override set after
    import (e.g. ``evals/run_eval.py`` for an A/B run). Unset/blank → the
    v1 default (``BOOK_CLASSIFY_PROMPT``); never anything else.

    An override value must be exactly ``<name>.md`` — a bare file name. It is
    resolved inside :data:`PROMPTS_DIR` (no subdirectories, no traversal, no
    other suffix) and the file's own ``prompt-version`` marker must equal its
    file stem, so ``LLM_PROMPT=book-classify-v2.md`` fails loudly if that file
    is missing, malformed, or carries any other version. Raises
    :class:`PromptFormatError` on every mismatch — the override never fails
    open to a different prompt than the operator named.
    """
    raw = os.getenv(PROMPT_ENV_VAR, "").strip()
    if not raw:
        return BOOK_CLASSIFY_PROMPT

    if not _PROMPT_ENV_PATTERN.match(raw):
        raise PromptFormatError(
            f"{PROMPT_ENV_VAR} must be a bare '<name>.md' file name "
            f"under prompts/, got: {raw!r}"
        )
    path = PROMPTS_DIR / raw
    version, _ = load_prompt(path)  # raises on unreadable/malformed files
    if version != path.stem:
        raise PromptFormatError(
            f"{PROMPT_ENV_VAR} points at {raw!r} but that file's "
            f"prompt-version marker is {version!r}, not {path.stem!r}"
        )
    return path
