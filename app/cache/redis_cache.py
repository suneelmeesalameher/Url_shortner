"""Redis implementation of `CachePort` (app.core.ports).

REFACTORED from the previous `app/core/cache.py` module of standalone
`safe_get`/`safe_set` functions into a class. That module already did the right
thing (circuit breaker + RedisError handling so a Redis outage degrades to "cache
miss" instead of raising) - the problem it had was one of *placement*, not
mechanics: it lived under `app.core` and was imported directly by
`app.services.url_service`, which meant the service layer's import list included a
Redis-specific module even though the service itself never touched a Redis client.
That's a Dependency Inversion violation (a high-level module depending on a
low-level module) hiding behind seemingly-fine code.

Wrapping the exact same mechanics in a class that satisfies `CachePort` fixes the
dependency direction without changing the resilience behavior at all: `UrlService`
now depends on the `CachePort` Protocol, and this class - the only place in the app
that imports `redis.asyncio` for caching purposes - is handed to it at the
composition root (app.api.dependencies).
"""
import logging

import redis.asyncio as redis
from redis.exceptions import RedisError

from app.core.circuit_breaker import redis_circuit_breaker

logger = logging.getLogger(__name__)


class RedisCache:
    """Adapts a request-scoped `redis.Redis` client to `CachePort`.

    Every failure mode - Redis down, timed out, whatever the shared circuit breaker
    reports - collapses to the same observable behavior the port promises: `get`
    returns None, `set` does nothing. Callers get a warning in the structured logs
    either way; they never get an exception from this class.
    """

    def __init__(self, client: redis.Redis):
        self._client = client

    async def get(self, key: str) -> str | None:
        if not redis_circuit_breaker.is_available():
            return None
        try:
            value = await self._client.get(key)
            await redis_circuit_breaker.record_success()
            return value
        except RedisError as exc:
            await redis_circuit_breaker.record_failure()
            logger.warning("Redis GET failed for key=%s (%s); falling back to Postgres", key, exc)
            return None

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if not redis_circuit_breaker.is_available():
            return
        try:
            await self._client.set(key, value, ex=ttl_seconds)
            await redis_circuit_breaker.record_success()
        except RedisError as exc:
            await redis_circuit_breaker.record_failure()
            logger.warning("Redis SET failed for key=%s (%s); continuing without caching", key, exc)
