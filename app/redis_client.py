"""Async Redis connection pool (cache layer plumbing)."""
from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.config import settings

_pool = redis.ConnectionPool.from_url(
    settings.redis_url,
    decode_responses=True,  # work with str instead of bytes throughout the app
    max_connections=50,
    # MODIFIED (reliability hardening): without an explicit timeout, redis-py waits on
    # the OS-level TCP timeout (can be tens of seconds) when Redis is unreachable. That
    # would mean every request hangs that long before the app.core.cache / rate_limiter
    # fallback logic even gets a chance to run - worse than having no cache at all. A
    # healthy Redis on the same network responds in well under a millisecond, so 200ms
    # is generous headroom, not a tight budget.
    socket_connect_timeout=settings.redis_connect_timeout_seconds,
    socket_timeout=settings.redis_socket_timeout_seconds,
)


def get_client() -> redis.Redis:
    """Build a client from the shared pool directly, without the async-generator
    dependency wrapper. Used by the rate-limit middleware, which runs outside
    FastAPI's Depends() machinery and so can't take get_redis() as a dependency.

    MODIFIED (test infrastructure): a pooled connection is bound to whatever event
    loop was running when it was first opened - fine in production (one process,
    one loop), but the test suite legitimately runs work across more than one loop
    (see app.db's identical NullPool comment for the same issue on the Postgres
    side). `redis_disable_shared_pool` (set only by tests/conftest.py) makes each
    call build a standalone client with its own private pool instead of sharing
    `_pool`; `aclose()` then fully closes that connection rather than returning it
    to a pool something else might reuse from a different, possibly-closed loop.
    """
    if settings.redis_disable_shared_pool:
        return redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
        )
    return redis.Redis(connection_pool=_pool)


async def get_redis() -> AsyncGenerator[redis.Redis, None]:
    """FastAPI dependency yielding a Redis client backed by the shared pool."""
    client = get_client()
    try:
        yield client
    finally:
        await client.aclose()
