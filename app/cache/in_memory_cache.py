"""A second `CachePort` implementation, holding entries in a process-local dict.

Not wired into production (see app.api.dependencies, which wires `RedisCache`) -
its purpose is to be concrete proof that the Open/Closed claim about the cache
abstraction actually holds: `UrlService` takes anything satisfying `CachePort` in
its constructor, so this class is a drop-in swap with **no changes to
`UrlService`, no changes to any route** - exercised directly in
tests/test_url_service_ports.py using this class instead of Redis. It would also
be a reasonable choice for local development or a single-instance deployment that
doesn't want a Redis dependency at all.

Trade-off if it were used in production: no sharing across app instances (each
process has its own cache) and no eviction beyond lazy expiry-on-read, so unbounded
distinct keys grow the dict unboundedly - fine for the local/dev/test use case this
was built for, not a substitute for Redis at real traffic volumes.
"""
import time


class InMemoryCache:
    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at_monotonic)

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._store[key] = (value, time.monotonic() + ttl_seconds)
