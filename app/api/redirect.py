"""Controller layer: the public redirect hot path (`GET /{short_code}`).

Deliberately mounted at the root (no /api prefix) - that's the entire point of a short URL.

REFACTORED (SOLID audit, SRP): this handler now does only HTTP translation - extract
short_code and request headers, call the service, shape the response. It no longer
constructs a ClickEvent (moved to UrlService.record_click - see that module's
docstring) and no longer runs rate-limit logic inline (moved to a dependency, see
app.api.dependencies.enforce_shortcode_rate_limit, declared below via the route's
`dependencies=` list so it runs - and can short-circuit with a 429 - before this
function's body executes at all).
"""
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import enforce_shortcode_rate_limit, get_url_service
from app.core.request_utils import get_client_ip
from app.db import get_db
from app.services.url_service import UrlService

router = APIRouter(tags=["redirect"])


@router.get("/{short_code}", dependencies=[Depends(enforce_shortcode_rate_limit)])
async def redirect_to_original_url(
    short_code: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    service: UrlService = Depends(get_url_service),
) -> RedirectResponse:
    resolved = await service.resolve_short_code(db, short_code)

    # Non-blocking: see UrlService.record_click - this only reads already-parsed
    # headers and pushes an object onto an in-memory queue, so it adds no
    # measurable latency here. The actual DB write happens later, off this request
    # entirely (see analytics_recorder).
    service.record_click(
        resolved,
        short_code,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        referrer=request.headers.get("referer"),
    )

    # 302 (not 301): a 301 would let browsers/CDNs cache the redirect and skip our
    # server entirely on repeat clicks, silently under-counting analytics. 302 forces
    # every click to hit this endpoint. The no-store headers stop overzealous caches
    # from treating a 302 as cacheable anyway.
    return RedirectResponse(
        url=resolved.original_url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )
