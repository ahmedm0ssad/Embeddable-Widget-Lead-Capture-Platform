"""Raw model-text parsing and schema validation for the classify pipeline.

Stage 3 contract: the model's reply is untrusted text. This module turns that
text into either a validated :class:`ClassifyOutput` or a precise list of
rejection reasons — it never crashes on weird input (fences, preambles,
trailing commentary, truncated or multiple objects) and never invents default
values to pretend success.
"""

import json
from typing import Any

from pydantic import ValidationError

from app.models.book_classify import ClassifyOutput

# Names of the exact output-shape fields the prompt v1 contract demands;
# anything else in a reply is an unexpected extra and fails validation.
_EXPECTED_FIELDS = frozenset(ClassifyOutput.model_fields)


class RawParseError(ValueError):
    """No JSON object could be extracted from the raw model text."""


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced JSON object from arbitrary model text.

    Handles markdown fences, preambles ("Sure! Here's the JSON:"), trailing
    commentary, escaped quotes/braces inside strings, and multiple objects in
    one reply (the first parseable object wins — the model replays its first
    decision, later objects are typically corrections or echoes).

    Raises :class:`RawParseError` when nothing balanced and object-shaped can
    be decoded; never raises anything else for malformed text.
    """
    if not isinstance(text, str):
        raise RawParseError(f"model output was not text (got {type(text).__name__})")

    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            candidate, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)  # truncated object → try the next one
            continue
        if isinstance(candidate, dict):
            return candidate
        idx = text.find("{", idx + 1)  # first balanced non-object → try the next

    raise RawParseError("no complete JSON object found in raw model output")


def validate_classify_payload(
    payload: dict[str, Any],
) -> tuple[ClassifyOutput | None, list[str]]:
    """Validate a parsed dict against the classify schema, collecting ALL errors.

    Returns ``(output, [])`` on success — with ``quality_flags`` sorted
    alphabetically for deterministic comparison (the model's flag order is
    unstable, e.g. ``vague_title`` may arrive before ``short_description``;
    sorting makes equal payloads compare equal for cache hits and tests) —
    or ``(None, errors)`` with every violation listed.

    Accepted laxness (documented choice): Pydantic v2's default lax mode
    coerces string-encoded numbers, so ``"confidence": "0.9"`` validates as
    ``0.9``. Model outputs often stringify numbers; rejecting that would fail
    schema-correct content, so coercion is accepted while true violations
    (string where a list is required, invalid enum values, out-of-range
    values, wrong/missing fields) are rejected.
    """
    if not isinstance(payload, dict):
        return None, [f"expected a JSON object, got {type(payload).__name__}"]

    errors: list[str] = []
    extra = sorted(set(payload) - set(_EXPECTED_FIELDS))
    if extra:
        errors.append(
            f"unexpected extra field(s): {', '.join(extra)} (schema allows exactly: "
            f"{', '.join(sorted(_EXPECTED_FIELDS))})"
        )

    try:
        output = ClassifyOutput.model_validate(payload)
    except ValidationError as exc:
        errors.extend(_format_error(err) for err in exc.errors())

    if errors:
        return None, errors
    return _normalized(output), errors


def _normalized(output: ClassifyOutput) -> ClassifyOutput:
    """Return the output with quality_flags in deterministic (alphabetical) order."""
    return output.model_copy(
        update={"quality_flags": sorted(output.quality_flags, key=lambda f: f.value)}
    )


def parse_and_validate(
    text: str,
) -> tuple[ClassifyOutput | None, list[str]]:
    """Parse raw model text then validate it; never raises.

    Returns ``(None, errors)`` where a parse failure becomes the single error
    ``"<parse error>"`` and validation failures list every schema violation.
    """
    try:
        payload = parse_json_object(text)
    except RawParseError as exc:
        return None, [str(exc)]
    return validate_classify_payload(payload)


def _format_error(err: dict[str, Any]) -> str:
    """One-line readable rendering of a single pydantic validation error.

    Includes the offending input value so the repair attempt (and the
    quarantine log) state exactly what was wrong, not just where.
    """
    loc = ".".join(str(part) for part in err.get("loc", ())) or "body"
    message = f"{loc}: {err.get('msg', 'invalid value')}"
    if "input" in err:
        message += f" (got {err['input']!r})"
    return message
