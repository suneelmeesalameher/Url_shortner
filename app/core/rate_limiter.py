"""Redis-backed token-bucket rate limiter.

Why token bucket over a sliding-window log: a sliding-window log (a Redis ZSET
holding one entry per request, trimmed to the window on each check) is more
precise, but its memory footprint and per-request cost both scale with the request
rate itself - exactly the dimension we're trying to protect against under high
traffic. A token bucket holds two numbers per key (tokens, last-refill-time)
regardless of request volume, and each check is a single O(1) Redis round trip.
It also naturally allows brief bursts up to the bucket capacity, which is the
behavior you want for legitimate traffic (a user's browser retrying, a burst of
clicks on a link someone just shared) rather than hard-cutting at a rigid boundary.

Correctness under concurrency: bucket refill + debit is a read-modify-write, so it
has to be atomic or concurrent requests race and over-admit. The whole operation
runs as a single Lua script (EVALSHA, loaded once and cached by SHA - falling back
to EVAL on a cache miss e.g. after a Redis restart) so Redis executes it as one
atomic step; no separate GET-then-SET round trip that another request could
interleave with.

Fallback: if Redis is unavailable (per the shared circuit breaker) or a call fails,
this fails OPEN - the request is allowed through rather than blocked. A rate
limiter that fails closed would turn a Redis outage into a full service outage,
which is the opposite of the reliability goal driving this change; letting
unlimited traffic through for the (short, bounded) duration of an outage is the
safer failure mode.
"""
import logging
import time
from dataclasses import dataclass

import redis.asyncio as redis
from redis.exceptions import NoScriptError, RedisError

from app.core.circuit_breaker import redis_circuit_breaker

logger = logging.getLogger(__name__)

# KEYS[1] = bucket key
# ARGV[1] = capacity (max tokens, i.e. the burst limit)
# ARGV[2] = refill_rate (tokens per second = limit / window_seconds)
# ARGV[3] = now (unix seconds, float)
# ARGV[4] = requested tokens (1 per request)
_TOKEN_BUCKET_SCRIPT = """
local tokens_key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local bucket = redis.call('HMGET', tokens_key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local last_ts = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_ts = now
end

local elapsed = math.max(0, now - last_ts)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
local wait_seconds = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    wait_seconds = (requested - tokens) / refill_rate
end

redis.call('HMSET', tokens_key, 'tokens', tokens, 'ts', now)
-- Key can safely expire once a full bucket would have refilled anyway - nothing is
-- lost by treating an idle key as "back to full capacity" on its next use.
redis.call('EXPIRE', tokens_key, math.ceil(capacity / refill_rate) * 2)

return {allowed, tostring(wait_seconds)}
"""

_script_sha: str | None = None


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: float


async def _eval_token_bucket(
    client: redis.Redis, key: str, capacity: float, refill_rate: float, now: float
) -> tuple[int, float]:
    global _script_sha
    if _script_sha is None:
        _script_sha = await client.script_load(_TOKEN_BUCKET_SCRIPT)

    try:
        allowed, wait_seconds = await client.evalsha(_script_sha, 1, key, capacity, refill_rate, now, 1)
    except NoScriptError:
        # Redis restarted (or flushed its script cache) since we loaded it - reload and retry once.
        _script_sha = await client.script_load(_TOKEN_BUCKET_SCRIPT)
        allowed, wait_seconds = await client.evalsha(_script_sha, 1, key, capacity, refill_rate, now, 1)

    return int(allowed), float(wait_seconds)


async def is_allowed(client: redis.Redis, key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Check-and-consume one token for `key`. `limit` requests are allowed per
    `window_seconds` (e.g. limit=100, window_seconds=60 -> ~100 req/min, refilling
    continuously rather than in a hard-reset fixed window).
    """
    if not redis_circuit_breaker.is_available():
        return RateLimitResult(allowed=True, retry_after_seconds=0.0)

    refill_rate = limit / window_seconds
    try:
        allowed, wait_seconds = await _eval_token_bucket(client, key, float(limit), refill_rate, time.time())
        await redis_circuit_breaker.record_success()
        return RateLimitResult(allowed=bool(allowed), retry_after_seconds=wait_seconds)
    except RedisError as exc:
        await redis_circuit_breaker.record_failure()
        logger.warning("Rate limiter Redis call failed for key=%s (%s); failing open", key, exc)
        return RateLimitResult(allowed=True, retry_after_seconds=0.0)
