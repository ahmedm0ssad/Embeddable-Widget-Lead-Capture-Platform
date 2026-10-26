from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response

from app.services import embed_service
from app.services.widget_js import render_widget_js

router = APIRouter(prefix="/public/widget", tags=["public-widget"])


@router.get(
    "/{widget_id}/config",
    summary="Get public widget config",
    description="Returns the public configuration JSON for an active widget, used by the embed script to render its form. Served with a short-lived cache header so the browser can reuse it without re-fetching on every page load. Returns 404 if the widget does not exist.",
)
async def get_widget_config(widget_id: UUID):
    config = await embed_service.get_widget_config(str(widget_id))
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget not found",
        )
    return JSONResponse(
        content=config,
        headers={
            "Cache-Control": "public, max-age=300",
        },
    )


@router.get(
    "/{widget_id}/widget.js",
    summary="Get embeddable widget JS bundle",
    description="Returns the JavaScript bundle that renders the widget on a third-party site. Served with an immutable one-year cache; returns 404 if the widget is missing and 410 if it has been deleted.",
)
async def get_widget_js(
    widget_id: UUID,
    v: int | None = Query(None),
):
    raw = await embed_service.get_raw_widget(str(widget_id))
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget not found",
        )

    active = raw.get("active", False)
    if not active:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Widget has been deleted",
        )

    config = raw.get("config", {})
    js_version = raw.get("js_version", 1)
    js_content = render_widget_js(str(widget_id), config, js_version)

    return Response(
        content=js_content,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
