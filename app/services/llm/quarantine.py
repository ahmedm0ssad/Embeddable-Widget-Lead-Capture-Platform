"""Quarantine log for model answers that failed validation (Stage 3).

One JSON object per line in ``logs/quarantine.jsonl`` (the ``logs`` dir is
git-ignored). A line is written ONLY when the pipeline gives up — i.e. after
the single repair attempt also failed, or a cache-hit blob no longer
validates — never for a failure that a repair recovered from.

Schema per line::

    ts             UTC ISO-8601 timestamp
    input          the original user payload (title/description dict)
    raw_output     the raw model text that failed
    errors         the exact validation/parse errors
    prompt_version prompt version in force at call time
    model          model id used
    attempt        "initial" | "repair" | "cache-hit"

Best-effort by design: a quarantine write must never take the endpoint down,
so file errors are swallowed (with the reasoning commented at the call site).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
LOG_PATH = _REPO_ROOT / "logs" / "quarantine.jsonl"


def log_failure(
    *,
    input_payload: dict[str, Any],
    raw_output: str,
    errors: list[str],
    prompt_version: str,
    model: str,
    attempt: str,
    path: Path = LOG_PATH,
) -> None:
    """Append one quarantine line. Never raises."""
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "input": input_payload,
        "raw_output": raw_output,
        "errors": errors,
        "prompt_version": prompt_version,
        "model": model,
        "attempt": attempt,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        # Quarantine is a diagnostic, not a dependency: if the log file cannot
        # be written (permissions, full disk, read-only FS), the classify
        # error itself (422) is what the caller needs — swallow the write
        # failure. Broad except is intentional; see pyproject per-file-ignores.
        pass
