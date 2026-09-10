"""Background worker that drains the click-event queue and batch-writes to Postgres.

Started once at app startup and run for the lifetime of the process (see the
lifespan handler in app.main). Batching turns "one DB write per click" into
"one DB write per ~second (or per N clicks)", which is what makes this scale
independently of redirect volume.
"""
import asyncio
import logging
from collections import Counter

from app.db import AsyncSessionLocal
from app.repositories import analytics_repository
from app.services.analytics_queue import ClickEvent, click_event_queue

logger = logging.getLogger(__name__)

_DEFAULT_FLUSH_INTERVAL_SECONDS = 1.0
_DEFAULT_BATCH_SIZE = 500


async def run_analytics_worker(
    flush_interval: float = _DEFAULT_FLUSH_INTERVAL_SECONDS,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> None:
    """Loop forever: wait up to `flush_interval` for the next event, then drain
    whatever else is already sitting in the queue (up to `batch_size`) and flush.
    On cancellation (app shutdown), flush any buffered events before exiting so a
    graceful shutdown doesn't silently drop the last partial batch.
    """
    buffer: list[ClickEvent] = []
    try:
        while True:
            try:
                event = await asyncio.wait_for(click_event_queue.get(), timeout=flush_interval)
                buffer.append(event)
                while len(buffer) < batch_size:
                    try:
                        buffer.append(click_event_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                pass  # nothing arrived in time; fall through and flush whatever we have

            if buffer:
                await _flush(buffer)
                buffer = []
    except asyncio.CancelledError:
        if buffer:
            await _flush(buffer)
        raise


async def _flush(events: list[ClickEvent]) -> None:
    try:
        async with AsyncSessionLocal() as db:
            await analytics_repository.bulk_insert_events(db, events)
            clicks_per_url = Counter(event.url_id for event in events)
            await analytics_repository.bulk_increment_click_counts(db, clicks_per_url)
    except Exception:
        # A flush failure should never crash the worker loop - log and keep going,
        # accepting the lost batch rather than backing up the queue indefinitely.
        logger.exception("Failed to flush %d analytics events", len(events))
