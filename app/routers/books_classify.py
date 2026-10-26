import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.models.book_classify import ClassifyInput, ClassifyOutput
from app.services.book_classify_service import classify_book

router = APIRouter(tags=["books"])


@router.post(
    "/books/classify",
    # Stage 3: every path (stub and real model) returns validated schema
    # output — parse/validate/repair/quarantine lives in the pipeline.
    response_model=ClassifyOutput,
    status_code=200,
    summary="Classify a scraped book record",
)
async def classify(request: Request) -> ClassifyOutput:
    """Validate the raw body manually (400, not 422) before any model/service call."""
    raw = await request.body()
    if not raw:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "fields": {"body": "Request body is required"},
            },
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_json",
                "message": "JSON decode error",
                "fields": {},
            },
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_failed",
                "fields": {"body": "Request body must be a JSON object"},
            },
        )

    try:
        data = ClassifyInput.model_validate(payload)
    except ValidationError as exc:
        fields: dict[str, str] = {}
        for err in exc.errors():
            field = err["loc"][0] if err["loc"] else "body"
            fields.setdefault(field, str(err["msg"]))
        raise HTTPException(
            status_code=400,
            detail={"error": "validation_failed", "fields": fields},
        ) from exc

    return classify_book(title=data.title, description=data.description)
