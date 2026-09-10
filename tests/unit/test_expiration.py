"""Unit tests for expiration/deactivation logic in UrlService.resolve_short_code.

This rule isn't a standalone function - it's a few lines of business logic inside
the service (`is_expired = not url_row.is_active or (expires_at is not None and
expires_at <= now)`) - so it's exercised here through UrlService with the fake
repository, isolating it from Postgres/Redis exactly like tests/unit/test_url_service_ports.py.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.cache.in_memory_cache import InMemoryCache
from app.core.exceptions import URLExpiredError
from app.services.url_service import UrlService
from tests.fakes import FakeUrlRepository


def _service():
    repo = FakeUrlRepository()
    return UrlService(repository=repo, cache=InMemoryCache()), repo


async def test_url_with_no_expiry_and_active_flag_resolves_successfully():
    service, repo = _service()
    row = await repo.create_url(
        db=None, url_id=1, short_code="abc123", original_url="https://example.com/a",
        expires_at=None, is_custom_alias=False,
    )
    resolved = await service.resolve_short_code(db=None, short_code=row.short_code)
    assert resolved.original_url == "https://example.com/a"


async def test_url_past_its_expires_at_raises_expired():
    service, repo = _service()
    row = await repo.create_url(
        db=None, url_id=1, short_code="abc123", original_url="https://example.com/a",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        is_custom_alias=False,
    )
    with pytest.raises(URLExpiredError):
        await service.resolve_short_code(db=None, short_code=row.short_code)


async def test_url_with_future_expires_at_still_resolves():
    service, repo = _service()
    row = await repo.create_url(
        db=None, url_id=1, short_code="abc123", original_url="https://example.com/a",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        is_custom_alias=False,
    )
    resolved = await service.resolve_short_code(db=None, short_code=row.short_code)
    assert resolved.original_url == "https://example.com/a"


async def test_deactivated_url_raises_expired_even_with_no_ttl_set():
    # is_active=False (a soft-deleted link) must be treated the same as a TTL expiry,
    # regardless of whether expires_at was ever set.
    service, repo = _service()
    row = await repo.create_url(
        db=None, url_id=1, short_code="abc123", original_url="https://example.com/a",
        expires_at=None, is_custom_alias=False,
    )
    row.is_active = False

    with pytest.raises(URLExpiredError):
        await service.resolve_short_code(db=None, short_code=row.short_code)


async def test_expired_lookup_is_tombstoned_so_repository_is_not_queried_twice():
    """Verifies the cache-stampede protection: once a short code is found expired,
    the result is cached as a tombstone so a second lookup for the same code is
    served from cache instead of hitting the repository again.
    """
    service, repo = _service()
    row = await repo.create_url(
        db=None, url_id=1, short_code="abc123", original_url="https://example.com/a",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        is_custom_alias=False,
    )

    with pytest.raises(URLExpiredError):
        await service.resolve_short_code(db=None, short_code=row.short_code)
    assert repo.get_by_short_code_call_count == 1

    with pytest.raises(URLExpiredError):
        await service.resolve_short_code(db=None, short_code=row.short_code)
    # Second lookup should be served from the tombstone cache entry, not the repository.
    assert repo.get_by_short_code_call_count == 1
