"""A minimal circuit breaker guarding all Redis access.

Why this exists: wrapping each Redis call in try/except is enough to stop a single
failed call from crashing a request, but it's not enough on its own during a real
outage. Redis is fronted by a short socket timeout (see app.redis_client) so a single
failed call fails fast - but under high traffic, "fails fast" still means every one
of thousands of requests per second pays that timeout before falling back to
Postgres, which is itself extra load on the DB at the worst possible moment.

The breaker tracks consecutive failures; once a threshold is crossed it "opens" for a
cooldown window, during which callers skip Redis entirely (no network call, no
timeout) and go straight to their Postgres fallback. After the cooldown it goes
half-open, allowing exactly the next call through as a trial - a success closes the
breaker again, a failure re-opens it for another full cooldown.

One process-wide instance (`redis_circuit_breaker`) is shared by the cache-aside
lookup in url_service and the rate limiter, since both depend on the same Redis and
should agree on whether it's currently considered healthy.
"""
import asyncio
import time
from enum import Enum

from app.config import settings


class CircuitState(Enum):
    CLOSED = "closed"  # healthy - calls go through normally
    OPEN = "open"  # tripped - calls are skipped entirely until the cooldown elapses
    HALF_OPEN = "half_open"  # cooldown elapsed - the next call is a trial


class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_timeout_seconds: float):
        self._failure_threshold = failure_threshold
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if time.monotonic() - self._opened_at >= self._recovery_timeout_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def is_available(self) -> bool:
        """True unless the breaker is OPEN. HALF_OPEN still returns True - that trial
        call is exactly what lets the breaker discover Redis has recovered."""
        return self.state != CircuitState.OPEN

    async def record_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()


redis_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.circuit_breaker_failure_threshold,
    recovery_timeout_seconds=settings.circuit_breaker_recovery_timeout_seconds,
)
