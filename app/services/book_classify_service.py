"""Book classification service (A17).

Thin entry point the router calls. All flow lives in
``app/services/llm/pipeline.py``: the versioned prompt, the Stage 1 stub
branch (``LLM_STUB=1`` — signature and classification behavior unchanged),
the request cache, and the real provider path with Stage 3's
parse → validate → once-repair → quarantine chain. Keeping the boundary here
means the router surface and these tests stay stable across stages.

Since Stage 3 the contract is schema-only everywhere:

    classify_book(title, description) -> ClassifyOutput
"""

from app.models.book_classify import ClassifyOutput
from app.services.llm.pipeline import run_book_classify


def classify_book(title: str, description: str | None) -> ClassifyOutput:
    """Classify a scraped book record.

    Returns the deterministic stub answer when ``LLM_STUB=1``; otherwise the
    LLM pipeline (prompt → cache → provider → parse/validate/repair). Raises
    ``HTTPException(503)`` when the model is unavailable/unconfigured and
    ``HTTPException(422)`` when the model's answer fails schema validation
    even after the single repair attempt.
    """
    return run_book_classify(title=title, description=description)
