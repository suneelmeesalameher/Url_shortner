"""Controller layer: management API (`/api/v1/...`).

Routes only translate HTTP <-> DTOs and delegate everything else to the service layer.
Domain exceptions raised by the service bubble up to the handlers registered in
app.main, which is where the actual HTTP status-code mapping lives.

REFACTORED: routes now depend on `UrlService` (app.services.url_service) via
`Depends(get_url_service)` instead of calling module-level functions - the service
is resolved with its concrete Postgres/Redis adapters injected at
app.api.dependencies (the composition root), but this module never sees those
concrete types, only the `UrlService` it was handed.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_url_service
from app.db import get_db
from app.schemas import AnalyticsResponse, ShortenRequest, ShortenResponse
from app.services.url_service import UrlService

router = APIRouter(prefix="/api/v1", tags=["urls"])


@router.post("/shorten", response_model=ShortenResponse, status_code=status.HTTP_201_CREATED)
async def shorten_url(
    payload: ShortenRequest,
    db: AsyncSession = Depends(get_db),
    service: UrlService = Depends(get_url_service),
) -> ShortenResponse:
    return await service.create_short_url(db, payload)


@router.get("/urls/{short_code}/analytics", response_model=AnalyticsResponse)
async def get_url_analytics(
    short_code: str,
    granularity: Literal["hour", "day"] = Query("day", description="Bucket size for the time series"),
    date_from: datetime | None = Query(None, alias="from", description="Range start, defaults to 7 days ago"),
    date_to: datetime | None = Query(None, alias="to", description="Range end, defaults to now"),
    db: AsyncSession = Depends(get_db),
    service: UrlService = Depends(get_url_service),
) -> AnalyticsResponse:
    end = date_to or datetime.now(timezone.utc)
    start = date_from or (end - timedelta(days=7))
    return await service.get_url_analytics(db, short_code, granularity, start, end)
