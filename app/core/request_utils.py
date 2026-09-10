"""Shared request-inspection helpers.

Extracted out of app.api.redirect so the new IP-based rate-limit middleware can
identify the same client IP the same way the analytics recorder already does -
two different rate limits keyed on two different IP-parsing rules would be a bug.
"""
from fastapi import Request


def get_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # Leftmost entry is the original client when set by a trusted reverse proxy/LB
        # in front of this service. If this app is ever exposed directly to the
        # internet without such a proxy, this header is attacker-controlled and must
        # not be trusted - that trust boundary belongs in the LB/proxy config, not here.
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else None
