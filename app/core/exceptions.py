"""Domain-level exceptions raised by the service layer.

The API layer never inspects DB/cache errors directly - it only ever catches these,
which keeps HTTP status-code mapping in one place (see app.main exception handlers).
"""


class ShortenerError(Exception):
    """Base class for all domain errors raised by this service."""


class InvalidURLError(ShortenerError):
    """Raised when the submitted URL or alias fails validation (bad syntax, SSRF target, etc.)."""


class DuplicateAliasError(ShortenerError):
    """Raised when a requested custom alias is already taken (DB unique-constraint violation)."""

    def __init__(self, alias: str):
        self.alias = alias
        super().__init__(f"Alias '{alias}' is already in use")


class URLNotFoundError(ShortenerError):
    """Raised when a short code has no matching row at all."""

    def __init__(self, short_code: str):
        self.short_code = short_code
        super().__init__(f"Short code '{short_code}' was not found")


class URLExpiredError(ShortenerError):
    """Raised when a short code exists but is past its TTL or has been deactivated."""

    def __init__(self, short_code: str):
        self.short_code = short_code
        super().__init__(f"Short code '{short_code}' has expired or is no longer active")


class StorageUnavailableError(ShortenerError):
    """Raised when the repository layer catches a low-level DB/network failure
    (connection refused, timeout, driver error) that isn't a business-meaningful
    outcome like a duplicate alias. Postgres is this app's source of truth with no
    further fallback, so a real outage can't be "gracefully degraded" the way a
    Redis outage can - the point of this exception is exception *isolation*, not
    exception *avoidance*: no repository method ever lets a raw SQLAlchemyError,
    OSError, or driver-specific exception escape past the repository boundary. The
    API layer maps this to a clean 503 with a structured body instead of leaking a
    stack trace as an unhandled 500.
    """

    def __init__(self, operation: str, cause: Exception):
        self.operation = operation
        super().__init__(f"Storage unavailable during '{operation}': {cause}")


class RateLimitExceededError(ShortenerError):
    """Raised when a request exceeds a shortcode-scoped rate limit.

    (The IP-scoped limit is enforced in middleware, ahead of routing, and returns its
    429 directly rather than going through this exception - see
    app.middleware.rate_limit. This exception exists for the shortcode-scoped limit,
    which is a business rule tied to the redirect operation and so lives in the
    service layer alongside it, consistent with every other domain error here.)
    """

    def __init__(self, key: str, retry_after_seconds: float):
        self.key = key
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded for '{key}'; retry after {retry_after_seconds:.1f}s")
