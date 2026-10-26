from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.dependencies.auth import get_current_user
from app.dependencies.services import get_widget_repo
from app.models.widget import (
    PaginatedResponse,
    WidgetCreate,
    WidgetResponse,
    WidgetUpdate,
)
from app.repositories.protocol import WidgetRepositoryProtocol
from app.services import widget_service
from app.services.widget_js import generate_script_tag

router = APIRouter(prefix="/widgets", tags=["widgets"])


@router.get(
    "/",
    response_model=PaginatedResponse,
    summary="List widgets",
    description="Lists the authenticated user's widgets with optional search, active filter, and pagination.",
)
async def list_widgets(
    search: str | None = Query(None),
    active: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
    repo: WidgetRepositoryProtocol = Depends(get_widget_repo),
):
    tenant_id = str(user["id"])
    items, total = await widget_service.get_widgets(
        tenant_id=tenant_id,
        search=search,
        active=active,
        page=page,
        page_size=page_size,
        repo=repo,
    )
    pages = (total + page_size - 1) // page_size if total > 0 else 0
    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.post(
    "/",
    response_model=WidgetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a widget",
    description="Creates a new widget for the authenticated user and returns it. The config accepts an optional webhook_url for submission notifications.",
)
async def create_widget(
    data: WidgetCreate,
    user: dict = Depends(get_current_user),
    repo: WidgetRepositoryProtocol = Depends(get_widget_repo),
):
    tenant_id = str(user["id"])
    return await widget_service.create_widget(data, tenant_id, repo=repo)


@router.get(
    "/{widget_id}",
    response_model=WidgetResponse,
    summary="Get a widget",
    description="Returns a single widget owned by the authenticated user. Returns 404 if it does not exist.",
)
async def get_widget(
    widget_id: UUID,
    user: dict = Depends(get_current_user),
    repo: WidgetRepositoryProtocol = Depends(get_widget_repo),
):
    tenant_id = str(user["id"])
    widget = await widget_service.get_widget(str(widget_id), tenant_id, repo=repo)
    if widget is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget not found",
        )
    return widget


@router.get(
    "/{widget_id}/embed",
    response_class=PlainTextResponse,
    summary="Get embed snippet HTML",
    description="Returns the one-line <script> tag that embeds a widget on a customer site. Uses the request host as the API base so the snippet always points at the API origin the owner reached. Returns 404 if the widget does not exist.",
)
async def get_widget_embed(
    widget_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    repo: WidgetRepositoryProtocol = Depends(get_widget_repo),
):
    tenant_id = str(user["id"])
    widget = await widget_service.get_widget(str(widget_id), tenant_id, repo=repo)
    if widget is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget not found",
        )
    base_url = str(request.base_url).rstrip("/")
    snippet = generate_script_tag(str(widget_id), widget.js_version, base_url=base_url)
    return PlainTextResponse(snippet, media_type="text/html")


@router.put(
    "/{widget_id}",
    response_model=WidgetResponse,
    summary="Update a widget",
    description="Updates a widget owned by the authenticated user and returns the updated widget. Returns 404 if it does not exist.",
)
async def update_widget(
    widget_id: UUID,
    data: WidgetUpdate,
    user: dict = Depends(get_current_user),
    repo: WidgetRepositoryProtocol = Depends(get_widget_repo),
):
    tenant_id = str(user["id"])
    widget = await widget_service.update_widget(
        str(widget_id), tenant_id, data, repo=repo
    )
    if widget is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget not found",
        )
    return widget


@router.delete(
    "/{widget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a widget",
    description="Soft-deletes a widget owned by the authenticated user. Returns 204 on success and 404 if it does not exist.",
)
async def delete_widget(
    widget_id: UUID,
    user: dict = Depends(get_current_user),
    repo: WidgetRepositoryProtocol = Depends(get_widget_repo),
):
    tenant_id = str(user["id"])
    deleted = await widget_service.delete_widget(str(widget_id), tenant_id, repo=repo)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget not found",
        )
