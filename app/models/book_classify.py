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
