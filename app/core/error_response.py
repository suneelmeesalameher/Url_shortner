"""Shared JSON error-body shape for exception handlers.

Extracted out of app.main so the new rate-limit middleware (which runs outside the
exception-handler machinery and has to build its own 429 response) can produce a
response identical in shape to every other error - callers shouldn't be able to tell
whether a 4xx came from a route handler's exception or from middleware.
"""
from datetime import datetime, timezone


def build_error_body(error_code: str, message: str) -> dict:
    return {"error": error_code, "message": message, "timestamp": datetime.now(timezone.utc).isoformat()}
