"""Structured (JSON) logging setup, with a per-request trace id threaded through
every log line via a ContextVar.

Every module in this app already logs through the stdlib `logging` module (see
app/cache/redis_cache.py, app/core/rate_limiter.py, app/services/analytics_recorder.py)
rather than `print()` - what was missing was configuration: with no handler attached,
those calls fell through to Python's "handler of last resort", which prints a bare,
unformatted message to stderr with no timestamp, no level, and no way to correlate
log lines from the same request. `configure_logging()` (called once at startup, see
app.main) replaces that with a single JSON-emitting handler on the root logger, so
every existing `logger.warning(...)` / `logger.exception(...)` call site starts
producing structured output with zero changes to those call sites.
"""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# Set by RequestContextMiddleware for the lifetime of a request; read here so every
# log record emitted during that request - from any module, any layer - carries the
# same trace id without every call site having to pass it around explicitly.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _RequestIdFilter(logging.Filter):
    """Stamps the current request id onto every record, read from the ContextVar."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_RequestIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
