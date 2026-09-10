"""Proves the SOLID audit's DIP/OCP claims with executable evidence rather than
just a docstring assertion: `UrlService` is exercised here with a fake in-memory
repository and the in-memory cache adapter - neither has ever touched Postgres or
Redis - and the full create -> resolve -> expire flow works with zero changes to
`UrlService` itself. That's the concrete demonstration that the service depends on
`UrlRepositoryPort`/`CachePort` (abstractions) and not on any concrete backend.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.cache.in_memory_cache import InMemoryCache
from app.core.exceptions import URLExpiredError, URLNotFoundError
from app.schemas import ShortenRequest
from app.services.url_service import UrlService
from tests.fakes import FakeUrlRepository


async def test_create_and_resolve_short_url_without_postgres_or_redis():
    service = UrlService(repository=FakeUrlRepository(), cache=InMemoryCache())

    response = await service.create_short_url(
        db=None, payload=ShortenRequest(original_url="https://example.com/page")
    )
    assert response.original_url == "https://example.com/page"

    resolved = await service.resolve_short_code(db=None, short_code=response.short_code)
    assert resolved.original_url == "https://example.com/page"


async def test_resolve_raises_not_found_for_unknown_code():
    service = UrlService(repository=FakeUrlRepository(), cache=InMemoryCache())
    with pytest.raises(URLNotFoundError):
        await service.resolve_short_code(db=None, short_code="doesnotexist")


async def test_resolve_raises_expired_once_ttl_has_passed():
    repo = FakeUrlRepository()
    service = UrlService(repository=repo, cache=InMemoryCache())

    response = await service.create_short_url(
        db=None,
        payload=ShortenRequest(
            original_url="https://example.com/page",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        ),
    )
    # Simulate TTL elapsing by mutating the fake's stored row directly.
    repo.rows[response.short_code].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(URLExpiredError):
        await service.resolve_short_code(db=None, short_code=response.short_code)


async def test_custom_alias_is_used_verbatim_as_the_short_code():
    service = UrlService(repository=FakeUrlRepository(), cache=InMemoryCache())

    response = await service.create_short_url(
        db=None,
        payload=ShortenRequest(original_url="https://example.com/page", custom_alias="my-brand"),
    )
    assert response.short_code == "my-brand"
