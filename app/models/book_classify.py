from enum import Enum

from pydantic import BaseModel, Field


class BookCategory(str, Enum):
    """Closed set of categories the classifier may return."""

    FICTION = "fiction"
    NON_FICTION = "non-fiction"
    MYSTERY_THRILLER = "mystery-thriller"
    SCI_FI_FANTASY = "sci-fi-fantasy"
    ROMANCE = "romance"
    HORROR = "horror"
    BIOGRAPHY = "biography"
    SELF_HELP = "self-help"
    OTHER = "other"


class QualityFlag(str, Enum):
    """Deterministic metadata-quality signals attached to a classification."""

    MISSING_DESCRIPTION = "missing_description"
    SHORT_DESCRIPTION = "short_description"
    VAGUE_TITLE = "vague_title"
    CONFLICTING_METADATA = "conflicting_metadata"


class ClassifyInput(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=2000)


class ClassifyOutput(BaseModel):
    category: BookCategory
    summary: str = Field(max_length=200)
    quality_flags: list[QualityFlag]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class RawClassifyOutput(BaseModel):
    """Stage 2 honest passthrough: the raw model text, unparsed.

    The real path at Stage 2 does not parse/validate the model output yet
    (that is Stage 3), so the endpoint returns ``{"raw": <model text>}``.
    """

    raw: str


#: Endpoint response shape: the full classification (stub mode / Stage 3+) or
#: the Stage 2 raw-text passthrough from the real model path.
ClassifyResponse = ClassifyOutput | RawClassifyOutput
