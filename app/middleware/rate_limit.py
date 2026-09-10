"""IP-based rate-limit middleware, applied globally ahead of routing.

This is a new addition (brownfield): every existing endpoint keeps its existing
request/response schema and status codes for the traffic it already accepted - the
only behavior change is that an IP sustaining more than `rate_limit_per_ip_per_minute`
requests now gets a 429 instead of being served. Nothing about a *compliant* client's
experience changes, which is what "backward compatible" means for a rate limit.

Applied as middleware (rather than a per-route dependency) because it's an IP-level,
cross-cutting concern that should apply uniformly before any route-specific logic
runs - identical to how a reverse proxy would apply it, just without new infra.
"""
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.core.error_response import build_error_body
from app.core.rate_limiter import is_allowed
from app.core.request_utils import get_client_ip
from app.redis_client import get_client

# Introspection routes are exempt: they're not part of the public API surface this
# limit is meant to protect, and rate-limiting /docs would just be a footgun for
# whoever's poking at the API interactively. /healthz is exempt too - an
# orchestrator polling it every few seconds (see the Dockerfile HEALTHCHECK and
# docker-compose.yml) shouldn't be able to trip a limit meant for API abuse.
# "/" (the dashboard's own HTML page - see GET / in app.main) is exempt for the
# same reason: loading the page is not the API traffic this limit protects. Note
# that the dashboard's own fetch() calls to /api/v1/... are deliberately NOT
# exempt - those go through the same limit as any other client.
_EXEMPT_PATHS = frozenset({"/", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect", "/healthz"})


class IPRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        client_ip = get_client_ip(request) or "unknown"
        redis_client = get_client()
        try:
            result = await is_allowed(
                redis_client,
                key=f"ratelimit:ip:{client_ip}",
                limit=settings.rate_limit_per_ip_per_minute,
                window_seconds=60,
            )
        finally:
            await redis_client.aclose()

        if not result.allowed:
            retry_after = max(1, int(result.retry_after_seconds))
            return JSONResponse(
                status_code=429,
                content=build_error_body(
                    "RATE_LIMITED", f"Too many requests from this client; retry after {retry_after}s"
                ),
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
