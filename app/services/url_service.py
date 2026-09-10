"""Business logic layer: validation + orchestration between the cache and the DB.

REFACTORED (SOLID audit):
- SRP: this module now does exactly one thing - the URL-shortening/redirect domain
  logic. Two things that used to leak in have been pulled out:
    1. Shortcode-scoped rate limiting used to be an inline check at the top of
       resolve_short_code(). Rate limiting is a cross-cutting traffic-shaping policy,
       not a rule about what a short code resolves to - it's now enforced by a
       FastAPI dependency (app.api.dependencies.enforce_shortcode_rate_limit) that
       runs before this method is even called, symmetric with how the IP-scoped
       limit is enforced by middleware before routing.
    2. Building a ClickEvent (deciding what a "click" is and handing it to the
       analytics queue) used to happen in the redirect controller. That's a business
       decision about the redirect operation, not HTTP translation, so it's now
       `UrlService.record_click()` - the controller just extracts raw request
       primitives (ip, user-agent, referrer) and passes them in.
- DIP: the class depends on `UrlRepositoryPort` and `CachePort` (app.core.ports),
  injected through the constructor. This module has no import of sqlalchemy or
  redis.asyncio - it doesn't know Postgres or Redis exist. Concrete adapters are
  wired up at app.api.dependencies (the composition root).
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import base62
from app.core.exceptions import InvalidURLError, URLExpiredError, URLNotFoundError
from app.core.ports import CachePort, UrlRepositoryPort
from app.core.validation import validate_and_sanitize_url, validate_custom_alias
from app.repositories import analytics_repository
from app.schemas import AnalyticsResponse, ReferrerCount, ShortenRequest, ShortenResponse, TimeBucket
from app.services.analytics_queue import ClickEvent, enqueue_click_event

# Sentinel cached for short codes that are known to be expired/inactive, so a hot,
# repeatedly-clicked dead link doesn't hammer the DB on every request.
_EXPIRED_TOMBSTONE = "__EXPIRED__"


@dataclass(frozen=True)
class ResolvedUrl:
    url_id: int
    original_url: str


def _cache_key(short_code: str) -> str:
    return f"url:{short_code}"


class UrlService:
    def __init__(self, repository: UrlRepositoryPort, cache: CachePort):
        self._repository = repository
        self._cache = cache

    async def create_short_url(self, db: AsyncSession, payload: ShortenRequest) -> ShortenResponse:
        original_url = validate_and_sanitize_url(payload.original_url)

        if payload.expires_at is not None and payload.expires_at <= datetime.now(timezone.utc):
            raise InvalidURLError("expires_at must be in the future")

        url_id = await self._repository.get_next_id(db)

        if payload.custom_alias:
            validate_custom_alias(payload.custom_alias)
            short_code = payload.custom_alias
            is_custom_alias = True
        else:
            short_code = base62.encode(url_id)
            is_custom_alias = False

        url_row = await self._repository.create_url(
            db,
            url_id=url_id,
            short_code=short_code,
            original_url=original_url,
            expires_at=payload.expires_at,
            is_custom_alias=is_custom_alias,
        )

        return ShortenResponse(
            short_code=url_row.short_code,
            short_url=f"{settings.base_host}/{url_row.short_code}",
            original_url=url_row.original_url,
            created_at=url_row.created_at,
            expires_at=url_row.expires_at,
        )

    async def resolve_short_code(self, db: AsyncSession, short_code: str) -> ResolvedUrl:
        """Return the target of a short code, or raise URLNotFoundError/URLExpiredError.

        Cache-aside: check the cache first; on miss, fall back to Postgres and
        repopulate the cache. The cached value carries url_id alongside the URL (as
        JSON) so a cache hit still gives the caller enough to record a click event -
        without that, a cache hit would have no way to attribute the click to a
        url_id without an extra DB query.

        Graceful degradation: this method has no idea whether `self._cache` is
        currently backed by a healthy Redis, a Redis that's down, or something else
        entirely satisfying CachePort - `get` returning None is indistinguishable
        from a real cache miss, by design (see CachePort's docstring). So a Redis
        outage falls through to the Postgres path below exactly like a first-ever
        request for a short code would, with no special-casing needed here.
        """
        cached_value = await self._cache.get(_cache_key(short_code))
        if cached_value is not None:
            if cached_value == _EXPIRED_TOMBSTONE:
                raise URLExpiredError(short_code)
            payload = json.loads(cached_value)
            return ResolvedUrl(url_id=payload["id"], original_url=payload["url"])

        url_row = await self._repository.get_by_short_code(db, short_code)
        if url_row is None:
            raise URLNotFoundError(short_code)

        now = datetime.now(timezone.utc)
        is_expired = not url_row.is_active or (url_row.expires_at is not None and url_row.expires_at <= now)

        if is_expired:
            await self._cache.set(_cache_key(short_code), _EXPIRED_TOMBSTONE, settings.expired_tombstone_ttl_seconds)
            raise URLExpiredError(short_code)

        ttl_seconds = settings.default_cache_ttl_seconds
        if url_row.expires_at is not None:
            # Never cache a live link past its own expiry, but keep it hard-floored at 1s.
            ttl_seconds = max(1, min(ttl_seconds, int((url_row.expires_at - now).total_seconds())))

        cache_payload = json.dumps({"id": url_row.id, "url": url_row.original_url})
        await self._cache.set(_cache_key(short_code), cache_payload, ttl_seconds)

        return ResolvedUrl(url_id=url_row.id, original_url=url_row.original_url)

    def record_click(
        self,
        resolved: ResolvedUrl,
        short_code: str,
        ip_address: str | None,
        user_agent: str | None,
        referrer: str | None,
    ) -> None:
        """Build and enqueue a click event. Non-blocking (see analytics_queue) - the
        redirect controller calls this after building its response, so it never
        adds latency to the redirect itself.
        """
        enqueue_click_event(
            ClickEvent(
                url_id=resolved.url_id,
                short_code=short_code,
                clicked_at=datetime.now(timezone.utc),
                ip_address=ip_address,
                user_agent=user_agent,
                referrer=referrer,
            )
        )

    async def get_url_analytics(
        self, db: AsyncSession, short_code: str, granularity: str, start: datetime, end: datetime
    ) -> AnalyticsResponse:
        """Read-side analytics query. Deliberately reads Postgres directly (no
        caching) - this is a low-QPS dashboard read, not the redirect hot path, so
        consistency matters more here than shaving milliseconds.
        """
        url_row = await self._repository.get_by_short_code(db, short_code)
        if url_row is None:
            raise URLNotFoundError(short_code)

        time_series_rows = await analytics_repository.get_clicks_over_time(db, url_row.id, granularity, start, end)
        referrer_rows = await analytics_repository.get_top_referrers(db, url_row.id, start, end)

        return AnalyticsResponse(
            short_code=url_row.short_code,
            # Total uses the denormalized counter (maintained by the analytics worker in
            # lockstep with the event inserts) instead of COUNT(*) over analytics_events,
            # which would mean scanning every event row on every dashboard load.
            total_clicks=url_row.click_count,
            time_series=[TimeBucket(bucket=bucket, clicks=clicks) for bucket, clicks in time_series_rows],
            top_referrers=[ReferrerCount(referrer=referrer, clicks=clicks) for referrer, clicks in referrer_rows],
        )
