"""Application entrypoint: FastAPI app assembly, router registration, lifecycle of
the background analytics worker, and the single place where domain exceptions get
translated into HTTP responses.
"""
import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.redirect import router as redirect_router
from app.api.v1.urls import router as urls_router
from app.config import settings
from app.core.error_response import build_error_body
from app.core.exceptions import (
    DuplicateAliasError,
    InvalidURLError,
    RateLimitExceededError,
    StorageUnavailableError,
    URLExpiredError,
    URLNotFoundError,
)
from app.core.logging_config import configure_logging
from app.db import Base, engine
from app.middleware.rate_limit import IPRateLimitMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.services.analytics_recorder import run_analytics_worker

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # DEV/DEMO CONVENIENCE, not a migration system: `create_all` is idempotent (it
    # checks what exists before creating anything), so running it on every startup
    # is safe, but it can only ever add tables/columns that match the current ORM
    # models - it has no concept of altering an existing column or a real migration
    # history. This is what lets `docker compose up` bring up a fully working stack
    # in one command without a separate manual setup step. A real production
    # rollout should replace this with Alembic migrations (already a dependency in
    # requirements.txt) run as a distinct release step, not on every app boot.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    worker_task = asyncio.create_task(
        run_analytics_worker(
            flush_interval=settings.analytics_flush_interval_seconds,
            batch_size=settings.analytics_flush_batch_size,
        )
    )
    try:
        yield
    finally:
        # Cancel and await so any events already buffered in the worker get flushed
        # (see the CancelledError handling in run_analytics_worker) instead of dropped.
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task


app = FastAPI(title="URL Shortener", version="1.0.0", lifespan=lifespan)

# Serves the single-page dashboard (static/index.html) - a thin client over the
# same public API, not a separate app. Mounted under /static rather than at the
# root so the root path itself can be routed explicitly (see "/" below) instead
# of falling through to StaticFiles' own directory-listing/index behavior.
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def serve_dashboard() -> FileResponse:
    """Excluded from the OpenAPI schema (`include_in_schema=False`) - this isn't
    part of the versioned API surface documented at /docs, it's the browser entry
    point for the dashboard that calls that API.
    """
    return FileResponse(os.path.join("static", "index.html"))


@app.get("/healthz", tags=["health"])
async def health_check() -> dict:
    """Plain liveness probe for container/orchestrator healthchecks (see the
    Dockerfile HEALTHCHECK and docker-compose.yml). Deliberately does not touch
    Postgres or Redis - it answers "is the app process up and serving requests",
    not "are its dependencies healthy" (which the app already degrades gracefully
    around at the request level - see StorageUnavailableError and the Redis
    circuit breaker). Registered before the redirect router's catch-all
    `GET /{short_code}` so this exact path always wins the route match.
    """
    return {"status": "ok"}

# MODIFIED (reliability hardening): global IP-based rate limiting ahead of routing.
# Existing endpoints are unaffected below the limit - see app/middleware/rate_limit.py.
app.add_middleware(IPRateLimitMiddleware)

# Added last so it ends up OUTERMOST (Starlette wraps middleware in reverse
# registration order - verified empirically, not assumed). Being outermost means the
# trace id is set before IPRateLimitMiddleware runs, so even a request rejected by
# the rate limiter gets a correlated, structured log line and an X-Request-ID header.
app.add_middleware(RequestContextMiddleware)


@app.exception_handler(InvalidURLError)
async def handle_invalid_url(request: Request, exc: InvalidURLError) -> JSONResponse:
    return JSONResponse(status_code=400, content=build_error_body("INVALID_URL", str(exc)))


@app.exception_handler(DuplicateAliasError)
async def handle_duplicate_alias(request: Request, exc: DuplicateAliasError) -> JSONResponse:
    return JSONResponse(status_code=409, content=build_error_body("ALIAS_TAKEN", str(exc)))


@app.exception_handler(URLNotFoundError)
async def handle_not_found(request: Request, exc: URLNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content=build_error_body("NOT_FOUND", str(exc)))


@app.exception_handler(URLExpiredError)
async def handle_expired(request: Request, exc: URLExpiredError) -> JSONResponse:
    return JSONResponse(status_code=410, content=build_error_body("GONE", str(exc)))


# MODIFIED (defensive programming / exception isolation): a Postgres outage or
# driver-level failure is now caught at the repository boundary (see
# PostgresUrlRepository, analytics_repository) and re-raised as this domain
# exception instead of propagating as a raw, unstructured 500. There is no further
# fallback below Postgres in this architecture (unlike the Redis->Postgres path),
# so the correct response is a clean 503 - "temporarily unavailable, the client's
# request was otherwise valid" - not a crash.
@app.exception_handler(StorageUnavailableError)
async def handle_storage_unavailable(request: Request, exc: StorageUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content=build_error_body("STORAGE_UNAVAILABLE", str(exc)))


# Handles the shortcode-scoped rate limit raised by the
# app.api.dependencies.enforce_shortcode_rate_limit dependency (the IP-scoped limit
# is handled directly inside IPRateLimitMiddleware instead, since it runs ahead of
# routing and needs to short-circuit before any dependency resolution happens).
@app.exception_handler(RateLimitExceededError)
async def handle_rate_limited(request: Request, exc: RateLimitExceededError) -> JSONResponse:
    retry_after = max(1, int(exc.retry_after_seconds))
    return JSONResponse(
        status_code=429,
        content=build_error_body("RATE_LIMITED", str(exc)),
        headers={"Retry-After": str(retry_after)},
    )


# Management API under /api/v1, public redirect at the root.
app.include_router(urls_router)
app.include_router(redirect_router)
