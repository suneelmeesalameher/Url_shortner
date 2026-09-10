"""Assigns a per-request trace id and makes it available to structured logging.

Registered as the outermost middleware (added last in app.main - see the comment
there on Starlette's middleware ordering) so the id is set before the rate limiter
or any route logic runs, meaning even a request rejected by the rate limiter gets
correlated log lines.
"""
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.logging_config import request_id_var


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Honor an inbound id from a load balancer/gateway if present, so a trace can
        # be followed across services; otherwise mint one for this request.
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        response.headers["X-Request-ID"] = request_id
        return response
