"""In-process click-event queue.

This is the seam between the redirect hot path and analytics persistence: the
redirect handler only ever calls `enqueue_click_event`, which is O(1) and does no
I/O, so it can never add latency to a redirect response. A background worker
(app.services.analytics_recorder) is the only consumer.

Trade-off: this queue lives in process memory, so a hard crash between enqueue and
flush loses buffered events, and it doesn't help if you scale to multiple app
instances (each has its own queue - fine, since each only carries the clicks it
personally served). If durability or a shared queue across instances is ever
needed, this module is the one place to swap for Redis Streams/Kafka/SQS; nothing
above it (the redirect handler) would need to change.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 10_000


@dataclass(frozen=True)
class ClickEvent:
    url_id: int
    short_code: str
    clicked_at: datetime
    ip_address: str | None
    user_agent: str | None
    referrer: str | None


click_event_queue: asyncio.Queue[ClickEvent] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)


def enqueue_click_event(event: ClickEvent) -> None:
    """Non-blocking enqueue. Under sustained overload (queue full - the worker can't
    keep up), the event is dropped rather than blocking the caller: a lost click
    count is a far smaller problem than a slow or failing redirect.
    """
    try:
        click_event_queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning("Analytics queue full; dropping click event for short_code=%s", event.short_code)
