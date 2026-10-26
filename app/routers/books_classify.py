import json
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.models.book_classify import ClassifyInput, ClassifyOutput
from app.services.book_classify_service import classify_book
from app.services.llm.streaming import StreamEvent, run_book_classify_stream

router = APIRouter(tags=["books"])


def _validated_classify_body(raw: bytes) -> ClassifyInput:
    """Manual raw-body validation shared by the sync and streaming routes.

    Every failure is an HTTP 400 naming the offending field(s) — this
    endpoint's contract is 400-with-``error`` for malformed input, never the
    FastAPI default 422. Running it in the route handler (not inside the
    stream generator) guarantees the 400 is a real HTTP response BEFORE any
    SSE frame can start.
    """
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
        return ClassifyInput.model_validate(payload)
    except ValidationError as exc:
        fields: dict[str, str] = {}
        for err in exc.errors():
            field = err["loc"][0] if err["loc"] else "body"
            fields.setdefault(field, str(err["msg"]))
        raise HTTPException(
            status_code=400,
            detail={"error": "validation_failed", "fields": fields},
        ) from exc


def _sse_frame(event: str, data: str) -> str:
    """One SSE frame: ``event: <name>`` + ``data: <line(s)>`` + blank line.

    A payload containing newlines becomes consecutive ``data:`` lines inside
    the same frame — SSE clients concatenate same-frame ``data`` lines with
    ``\n``, so multi-line deltas survive losslessly.
    """
    data_lines = data.split("\n")
    return (
        f"event: {event}\n" + "".join(f"data: {line}\n" for line in data_lines) + "\n"
    )


def _sse_bytes(events: Iterator[StreamEvent]) -> Iterator[str]:
    """Bridge pipeline StreamEvents to the SSE wire format."""
    for event in events:
        if event.kind == "token":
            if event.delta:
                yield _sse_frame("token", event.delta)
        elif event.kind == "result":
            # Terminal result: ALWAYS the validated schema JSON — raw model
            # text never leaves via this event (tokens are deltas only).
            yield _sse_frame("result", event.output.model_dump_json())
        else:  # "error" — terminal; detail mirrors the sync route's detail
            yield _sse_frame("error", json.dumps({"detail": event.detail}))


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
    data = _validated_classify_body(await request.body())
    return classify_book(title=data.title, description=data.description)


@router.post(
    "/books/classify/stream",
    # Streaming stretch: SSE with named events (token → result | error).
    # The HTTP response itself stays 200 once body validation passes — SSE
    # errors live IN-STREAM as the terminal ``error`` event (the detail text
    # carries the same wording the sync route uses for 400/422/503/504).
    status_code=200,
    summary="Classify a scraped book record (streamed, SSE)",
)
async def classify_stream(request: Request) -> StreamingResponse:
    """Same manual 400 validation as sync, then a ``text/event-stream`` body.

    Events: many ``token`` deltas, then exactly ONE terminal event —
    ``result`` (validated ``ClassifyOutput`` JSON) or ``error``
    (``{"detail": ...}``). The terminal result is always the validated
    schema; raw model text appears only as ``token`` deltas, never as the
    answer.
    """
    data = _validated_classify_body(await request.body())
    return _stream_response(
        run_book_classify_stream(title=data.title, description=data.description)
    )


def _stream_response(events: Iterator[StreamEvent]) -> StreamingResponse:
    """SSE response wrapper (headers keep proxies from buffering events)."""
    return StreamingResponse(
        _sse_bytes(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
