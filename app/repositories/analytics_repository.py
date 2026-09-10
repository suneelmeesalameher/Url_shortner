"""Data-access layer for analytics_events: batch writes from the recorder worker,
and aggregate reads for the analytics API endpoint.
"""
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import StorageUnavailableError
from app.models import AnalyticsEvent, Url
from app.services.analytics_queue import ClickEvent

# NOTE on scope: bulk_insert_events/bulk_increment_click_counts (below) are called
# only from the background analytics worker (app.services.analytics_recorder), which
# already wraps its whole flush in a broad try/except that logs and drops the batch
# on any failure - a second, narrower translation layer here wouldn't change that
# worker's behavior, so it's left as-is. get_clicks_over_time/get_top_referrers ARE
# on a request path (the analytics endpoint), so those get the same
# StorageUnavailableError isolation as PostgresUrlRepository.


async def bulk_insert_events(db: AsyncSession, events: list[ClickEvent]) -> None:
    if not events:
        return
    db.add_all(
        [
            AnalyticsEvent(
                url_id=event.url_id,
                short_code=event.short_code,
                clicked_at=event.clicked_at,
                ip_address=event.ip_address,
                user_agent=event.user_agent,
                referrer=event.referrer,
            )
            for event in events
        ]
    )
    await db.commit()


async def bulk_increment_click_counts(db: AsyncSession, clicks_per_url: dict[int, int]) -> None:
    """Apply one UPDATE per url_id in the batch (typically a handful of rows, not one
    per click) so a burst of clicks on the same link doesn't turn into N row-locks.
    """
    if not clicks_per_url:
        return
    for url_id, count in clicks_per_url.items():
        await db.execute(update(Url).where(Url.id == url_id).values(click_count=Url.click_count + count))
    await db.commit()


async def get_clicks_over_time(
    db: AsyncSession, url_id: int, granularity: str, start: datetime, end: datetime
) -> list[tuple[datetime, int]]:
    bucket = func.date_trunc(granularity, AnalyticsEvent.clicked_at).label("bucket")
    stmt = (
        select(bucket, func.count().label("clicks"))
        .where(AnalyticsEvent.url_id == url_id, AnalyticsEvent.clicked_at >= start, AnalyticsEvent.clicked_at < end)
        .group_by(bucket)
        .order_by(bucket)
    )
    try:
        result = await db.execute(stmt)
        return list(result.all())
    except SQLAlchemyError as exc:
        raise StorageUnavailableError("get_clicks_over_time", exc) from exc


async def get_top_referrers(
    db: AsyncSession, url_id: int, start: datetime, end: datetime, limit: int = 5
) -> list[tuple[str | None, int]]:
    stmt = (
        select(AnalyticsEvent.referrer, func.count().label("clicks"))
        .where(AnalyticsEvent.url_id == url_id, AnalyticsEvent.clicked_at >= start, AnalyticsEvent.clicked_at < end)
        .group_by(AnalyticsEvent.referrer)
        .order_by(func.count().desc())
        .limit(limit)
    )
    try:
        result = await db.execute(stmt)
        return list(result.all())
    except SQLAlchemyError as exc:
        raise StorageUnavailableError("get_top_referrers", exc) from exc
