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

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"
BOOK_CLASSIFY_PROMPT = PROMPTS_DIR / "book-classify-v1.md"

_VERSION_PATTERN = re.compile(r"<!--\s*prompt-version:\s*(\S+)\s*-->")
_SYSTEM_PROMPT_START = "<!---- SYSTEM-PROMPT-START ---->"
_SYSTEM_PROMPT_END = "<!---- SYSTEM-PROMPT-END ---->"


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
