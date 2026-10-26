"""Book classification service (A17).

Thin entry point the router calls. All flow lives in
``app/services/llm/pipeline.py``: the versioned prompt, the Stage 1 stub
branch (``LLM_STUB=1`` — signature and classification behavior unchanged),
the request cache, and the real provider path. Keeping the boundary here
means the router surface and these tests stay stable across stages:

    classify_book(title, description) -> ClassifyOutput | RawClassifyOutput

At Stage 2 the real model path returns the raw model text wrapped as
``RawClassifyOutput({"raw": ...})`` — parse/validate/repair is Stage 3.
"""

from app.models.book_classify import ClassifyOutput, RawClassifyOutput
from app.services.llm.pipeline import run_book_classify


def classify_book(
    title: str, description: str | None
) -> ClassifyOutput | RawClassifyOutput:
    """Classify a scraped book record.

    Returns the deterministic stub answer when ``LLM_STUB=1``; otherwise the
    LLM pipeline (prompt → cache → provider) — at Stage 2 the raw model text
    is returned unparsed as ``{"raw": ...}``.
    """
    return run_book_classify(title=title, description=description)
