# Architecture & Design Summary

## System Architecture & Data Flow

```
                         ┌─────────────────────────────┐
                         │   RequestContextMiddleware   │  outermost - stamps a trace id
                         └──────────────┬──────────────┘  before anything else runs
                                        │
                         ┌──────────────▼──────────────┐
                         │    IPRateLimitMiddleware      │  token bucket, per client IP
                         └──────────────┬──────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
   POST /api/v1/shorten      GET /{short_code}         GET /api/v1/urls/{code}/analytics
              │                         │                         │
              └───────────┬─────────────┴─────────────┬───────────┘
                          │  (controllers - HTTP translation only)
                          ▼
                    UrlService  (business logic; depends on Protocols, not concrete clients)
                     │       │
        UrlRepositoryPort  CachePort
                     │       │
        PostgresUrlRepository  RedisCache ── guarded by a shared CircuitBreaker
                     │       │
                Postgres    Redis

  Redirect path only, fire-and-forget:
     UrlService.record_click() → asyncio.Queue → background worker → batched INSERT → Postgres
```

### Component breakdown

| Component | Role |
|---|---|
| **FastAPI (async)** | HTTP layer; controllers only translate requests/responses and delegate to `UrlService` - no business logic, no direct DB/Redis calls. |
| **`UrlService`** | Business logic: URL validation, Base62 short-code assignment, cache-aside orchestration, expiration rules. Constructed with a `UrlRepositoryPort` and `CachePort` (Python `Protocol`s) rather than concrete Postgres/Redis types - Dependency Inversion in practice, not just in name. |
| **Redis (cache-aside)** | `RedisCache` (a `CachePort` implementation) fronts `urls` lookups keyed by short code, and backs the token-bucket rate limiter. Every call is wrapped by a shared circuit breaker. |
| **PostgreSQL (async, via asyncpg/SQLAlchemy)** | Source of truth for `urls` and `analytics_events`. Short-code IDs come from a Postgres `Sequence`, fetched in one query before the row is inserted. Connection pooling: `pool_size=20`, `max_overflow=10` in production. |
| **Background analytics worker** | An `asyncio.Task` started in the app's `lifespan`, draining an in-process `asyncio.Queue` of click events in time- or size-bounded batches and bulk-writing them to Postgres - decoupled entirely from the redirect request/response cycle. |
| **Composition root (`app/api/dependencies.py`)** | The only module that wires concrete adapters (`PostgresUrlRepository`, `RedisCache`) to the abstractions `UrlService` depends on. |

---

## Key Design Decisions & Trade-offs

### Base62 Sequence Encoding vs. Hash-Based Collisions

A hash-based scheme (random or hashed Base62, as many shorteners implement) has to generate a candidate code, attempt an insert, and retry on a unique-constraint violation when it collides - a probabilistic approach whose retry rate climbs as the keyspace fills, adding both extra round trips and unbounded tail latency under contention.

This system instead pulls the next value from a Postgres `Sequence` (`SELECT nextval(...)`) *before* constructing the row, Base62-encodes that integer locally, and writes the fully-formed row - including `short_code` - in a single `INSERT`. Uniqueness is structural (the sequence never repeats a value), not probabilistic, so there is no collision-check branch and no retry loop in the create path at all. One query gets the id; the encode is pure, in-memory, and instantaneous. This is what "zero collisions and low latency," from the original design brief, actually means in code rather than in aspiration.

### Cache-Aside & Tombstoning

The redirect path (`UrlService.resolve_short_code`) is a standard cache-aside: check Redis, fall back to Postgres on a miss, repopulate Redis on the way out. The gap in a naive version of this pattern is **cache penetration** - a request for a short code that's expired, deactivated, or was never valid gets a cache *miss* every single time, so a hot, repeatedly-hit dead link (or a scripted probe) hammers Postgres on every request instead of being absorbed by the cache at all.

The fix is a sentinel value, `_EXPIRED_TOMBSTONE = "__EXPIRED__"`, written to the same Redis key with a short TTL (`expired_tombstone_ttl_seconds`, default 300s) the first time a short code is found expired or inactive. Subsequent lookups for that code hit the cache, see the tombstone, and raise immediately - no second database round trip. This is the same mechanism whether the "miss" originated from a TTL expiry or a soft-deleted (`is_active=False`) row.

### 302 vs. 301 Redirects

A `301 Moved Permanently` is a standing invitation for the requesting browser or an intermediate CDN to cache the redirect and never contact this service again for that code - which means every click after the first, for that client, becomes invisible to the analytics pipeline. That's disqualifying for a service whose value proposition includes accurate click counts.

`302 Found` is not cached by default, so every click reaches the redirect handler and gets recorded. To close the gap where an overzealous cache might still keep a 302 around, every redirect response explicitly sets `Cache-Control: no-cache, no-store, must-revalidate` and `Pragma: no-cache` - belt-and-suspenders against exactly the failure mode a 301 would guarantee.

---

## Resilience & Fault Tolerance

**Graceful degradation during Redis downtime.** A single `CircuitBreaker` instance is shared between the cache-aside lookup and the rate limiter. After 5 consecutive Redis failures it "opens" for a 30-second cooldown, during which both subsystems skip the network call entirely rather than paying a repeated connection-timeout cost on every request - this matters specifically *under load*, where retrying a dead connection on every request would itself become a bottleneck. `RedisCache.get`/`set` degrade to "cache miss" / no-op on any failure; the rate limiter's `is_allowed` degrades to `allowed=True`. Neither ever raises past its boundary, so a Redis outage is observably identical, from the caller's side, to Redis being merely empty.

**Fail-open rate limiting.** Deliberately: a rate limiter that fails *closed* during an infrastructure incident would turn a cache outage into a full service outage, which is the opposite of the reliability goal. Legitimate traffic is never blocked because Redis had a bad moment.

**SSRF defense.** URLs are validated at creation time against a scheme allowlist (`http`/`https` only) and an IP blocklist covering loopback, private (RFC 1918), link-local (including the `169.254.169.254` cloud metadata address), reserved, and multicast ranges - closing the door on using the redirect as a probe against internal infrastructure that an automated consumer of the link (a bot, a crawler, an internal monitor) might have access to.

**Database connection pooling.** The async SQLAlchemy engine uses a bounded pool (`pool_size=20`, `max_overflow=10`) in production, with `pool_pre_ping=True` so a connection killed by the server (restart, failover) is detected and replaced rather than handed to a request as dead. (The test harness swaps this for `NullPool` specifically to avoid asyncpg connections leaking across the multiple event loops a mixed sync/async test suite legitimately uses - a distinct, test-only concern with no bearing on production behavior.)

**No fallback below Postgres - exception isolation instead.** Unlike Redis, there is nothing to fall back to if Postgres itself is unavailable. Every repository method wraps its `SQLAlchemyError` cases and re-raises a domain-specific `StorageUnavailableError`, which the API layer maps to a clean `503` with a structured error body. The distinction from the Redis story matters: this is defensive isolation of a real outage, not graceful degradation around one.

---

## Future Scalability

- **Sharded database sequences.** A single Postgres sequence is a fine bottleneck to have at moderate scale, but it is still a single point of write contention. The natural next step - previewed in this project's original architecture comparison - is a Snowflake-style identifier (timestamp + shard/node id + local sequence bits) so ID generation is horizontally distributed across write nodes with no shared counter at all.
- **Kafka (or Redis Streams) event streams for click logging.** The current click-event queue is an in-process `asyncio.Queue`, explicitly documented as a placeholder for this exact upgrade: durability across process restarts and fan-in from multiple app instances both require moving click events off single-process memory and onto a shared, durable log.
- **Multi-region read replicas.** The analytics read path (`GET /api/v1/urls/{code}/analytics`) and the redirect's cache-miss fallback are both read-heavy and latency-sensitive for geographically distributed traffic; routing those reads to regional replicas while keeping a single primary for `urls`/`analytics_events` writes would cut cross-region latency without touching the write-consistency model this design already relies on.
