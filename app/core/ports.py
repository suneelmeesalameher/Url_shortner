"""Abstractions the service layer depends on, instead of concrete Postgres/Redis types.

This is the Dependency Inversion boundary for the whole app: `UrlService` (the
high-level domain module) is typed against `UrlRepositoryPort` and `CachePort`
(structural `Protocol`s), never against `PostgresUrlRepository` or `RedisCache`
directly. Concrete adapters are wired in at `app.api.dependencies` (the composition
root) - that's the only place allowed to know which database or cache backend is
actually in use.

This also gives us Open/Closed for storage: a `DynamoDbUrlRepository` or an
`InMemoryCache` (see app/cache/in_memory_cache.py) can be added as a new class that
satisfies these Protocols, and `UrlService` needs zero changes to accept it - it's
closed for modification, open for extension via new adapters.

`typing.Protocol` (structural typing) is used rather than `abc.ABC` so adapters don't
need to inherit from anything - satisfying the method signatures is sufficient,
which keeps adapters free to also be, e.g., plain dataclasses or thin wrappers
around a third-party client.
"""
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Url


class UrlRepositoryPort(Protocol):
    """Persistence operations UrlService needs for the `urls` table.

    `db: AsyncSession` stays an explicit parameter (rather than something bound into
    the adapter at construction) because sessions are request-scoped in this app while
    the repository adapter itself is a stateless, process-lifetime singleton - the
    same shape the concrete Postgres adapter already had before this refactor.
    """

    async def get_next_id(self, db: AsyncSession) -> int: ...

    async def create_url(
        self,
        db: AsyncSession,
        *,
        url_id: int,
        short_code: str,
        original_url: str,
        expires_at: datetime | None,
        is_custom_alias: bool,
    ) -> Url: ...

    async def get_by_short_code(self, db: AsyncSession, short_code: str) -> Url | None: ...


class CachePort(Protocol):
    """Cache-aside operations UrlService needs.

    Deliberately just get/set - `UrlService` doesn't know or care whether the
    implementation is Redis, an in-memory dict, or something else, and it doesn't
    know about circuit breakers, retries, or Redis-specific exception types. A
    failure mode - Redis down, entry missing, whatever - surfaces uniformly as
    `get` returning None; `set` never raises. Enforcing that contract is the
    adapter's job (see app/cache/redis_cache.py), not the caller's.
    """

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
