"""Book classification service (A17).

STAGE 1: stub mode only — no model call is made yet. The real LLM pipeline
will live under ``app/services/llm/`` and plug in below, replacing the stub
branch while keeping this signature and the import surface stable:

    classify_book(title, description) -> ClassifyOutput
"""

import os

from fastapi import HTTPException

from app.models.book_classify import BookCategory, ClassifyOutput, QualityFlag

_STUB_REASON = "Stub mode: deterministic answer (no model call)."
_STUB_SUMMARY = (
    "A fictional narrative whose protagonist is drawn into a journey that "
    "reshapes how they understand their own life."
)


def classify_book(title: str, description: str | None) -> ClassifyOutput:
    """Classify a scraped book record.

    Returns a deterministic stub answer when ``LLM_STUB=1``; otherwise the
    real LLM integration (next stage) — for now a 503 signals it is not
    configured yet.
    """
    if os.getenv("LLM_STUB") == "1":
        # missing_description is deterministic local validation, not model work.
        quality_flags = (
            []
            if description and description.strip()
            else [QualityFlag.MISSING_DESCRIPTION]
        )
        return ClassifyOutput(
            category=BookCategory.FICTION,
            summary=_STUB_SUMMARY,
            quality_flags=quality_flags,
            confidence=0.9,
            reason=_STUB_REASON,
        )

    raise HTTPException(
        status_code=503,
        detail=(
            "LLM integration not configured (stub mode off) — "
            "this stage only implements the contract."
        ),
    )
