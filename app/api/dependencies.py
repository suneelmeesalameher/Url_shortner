"""Composition root: the one place concrete adapters get wired to the abstractions
they implement, and handed to controllers as FastAPI dependencies.

This is where Dependency Inversion actually gets *resolved* - `UrlService` only
knows about `UrlRepositoryPort`/`CachePort` (app.core.ports); this module is what
decides those are currently `PostgresUrlRepository`/`RedisCache`. Swapping either
backend (e.g. to `InMemoryCache` for a Redis-less deployment, or a future
`DynamoDbUrlRepository`) means changing exactly the two lines below that construct
them - no other file in the app needs to change.

Also hosts the shortcode-scoped rate limit as a FastAPI dependency rather than
inline in a route or in UrlService - see the SRP note in app.services.url_service's
module docstring for why it was moved out of the service, and app.middleware.rate_limit
for the IP-scoped counterpart this is symmetric with.
"""
import redis.asyncio as redis
from fastapi import Depends

from app.cache.redis_cache import RedisCache
from app.config import settings
from app.core.exceptions import RateLimitExceededError
from app.core.rate_limiter import is_allowed
from app.redis_client import get_redis
from app.repositories.postgres_url_repository import PostgresUrlRepository
from app.services.url_service import UrlService

# Stateless singleton: this adapter holds no per-request state (every method takes
# the request-scoped `AsyncSession` explicitly - see UrlRepositoryPort), so one
# instance for the process lifetime is correct and avoids reallocating it per request.
_url_repository = PostgresUrlRepository()


def get_url_service(cache_client: redis.Redis = Depends(get_redis)) -> UrlService:
    cache = RedisCache(cache_client)
    return UrlService(repository=_url_repository, cache=cache)


async def enforce_shortcode_rate_limit(
    short_code: str, cache_client: redis.Redis = Depends(get_redis)
) -> None:
    """FastAPI dependency: caps requests to a single short code (default 1000/min),
    independent of and in addition to the IP-scoped limit in middleware. Protects
    against one code being hammered (scripted abuse, or a cache-stampede-triggering
    flood) regardless of how many distinct IPs the traffic comes from.

    Fails open on a Redis outage - see app.core.rate_limiter.is_allowed, which
    never raises and returns `allowed=True` if the circuit breaker reports Redis
    unavailable.
    """
    result = await is_allowed(
        cache_client,
        key=f"ratelimit:shortcode:{short_code}",
        limit=settings.rate_limit_per_shortcode_per_minute,
        window_seconds=60,
    )
    if not result.allowed:
        raise RateLimitExceededError(short_code, result.retry_after_seconds)
