"""Centralized application configuration, loaded from environment variables / .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/url_shortener"
    redis_url: str = "redis://localhost:6379/0"
    # Test-only flag - see app.redis_client.get_client for why. Never set true in production.
    redis_disable_shared_pool: bool = False
    # Test-only flag - see app.db for why. Never set true in production.
    database_use_null_pool: bool = False

    # Used to build the fully-qualified short URL returned to clients.
    base_host: str = "http://localhost:8000"

    # Cache-aside TTLs for the redirect hot path.
    default_cache_ttl_seconds: int = 24 * 60 * 60  # 24h for links with no explicit expiry
    expired_tombstone_ttl_seconds: int = 300  # short-lived "known gone" marker to stop cache-miss storms

    custom_alias_min_length: int = 3
    custom_alias_max_length: int = 20

    # Rate limiting (token bucket - see app.core.rate_limiter).
    rate_limit_per_ip_per_minute: int = 100
    # Higher ceiling than the per-IP limit: a legitimately popular link gets clicked
    # by many distinct IPs, so this exists to cap abuse of one specific short code
    # (e.g. a scripted attack hammering a single URL) rather than to throttle normal
    # aggregate popularity.
    rate_limit_per_shortcode_per_minute: int = 1000

    # Short, aggressive Redis timeouts are what make the "fall back to Postgres
    # without crashing" story actually work under load - see app.redis_client.
    redis_connect_timeout_seconds: float = 0.2
    redis_socket_timeout_seconds: float = 0.2

    # Circuit breaker guarding all Redis access - see app.core.circuit_breaker.
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout_seconds: float = 30.0

    # Background analytics worker batching (see app.services.analytics_recorder).
    # Overridable so the integration test suite can flush near-instantly instead of
    # waiting on the production default - the batching *behavior* under test is the
    # same either way, only the interval changes.
    analytics_flush_interval_seconds: float = 1.0
    analytics_flush_batch_size: int = 500

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
