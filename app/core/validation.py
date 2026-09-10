"""Input sanitization and SSRF-guard validation.

Note on threat model: this service doesn't fetch the target URL itself, so classic
server-side SSRF (the app server making the request) isn't in play. The real risk is
*abuse of the redirect as an open door* into internal networks - e.g. an attacker
shortens `http://169.254.169.254/latest/meta-data` (cloud metadata endpoint) or
`http://localhost:9200` (an internal admin panel/DB) and distributes the short link to
internal automation, monitoring bots, or link-preview crawlers that *do* have network
access to those targets. Blocking loopback/private/link-local targets at creation time
closes that door.
"""
import ipaddress
import re
from urllib.parse import urlparse

from app.core.exceptions import InvalidURLError

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
_CUSTOM_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_and_sanitize_url(raw_url: str) -> str:
    """Validate URL syntax and block loopback/private/internal targets. Returns the trimmed URL."""
    candidate = raw_url.strip()
    if not candidate:
        raise InvalidURLError("URL must not be empty")

    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise InvalidURLError("Malformed URL") from exc

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise InvalidURLError(f"Unsupported URL scheme '{parsed.scheme}'; only http/https are allowed")

    hostname = parsed.hostname
    if not hostname:
        raise InvalidURLError("URL is missing a host")

    if hostname.lower() in _BLOCKED_HOSTNAMES:
        raise InvalidURLError("URLs pointing to localhost are not allowed")

    # If the host is a literal IP, reject anything in a private/loopback/link-local/reserved range
    # (covers RFC1918 space, 127.0.0.0/8, and the 169.254.0.0/16 cloud-metadata range).
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass  # not a literal IP - it's a hostname, nothing further to check here
    else:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise InvalidURLError("URLs pointing to private/internal IP addresses are not allowed")

    return candidate


def validate_custom_alias(alias: str) -> None:
    """Ensure a custom alias only contains URL-safe characters (length is enforced by the schema)."""
    if not _CUSTOM_ALIAS_PATTERN.match(alias):
        raise InvalidURLError(
            "Custom alias may only contain letters, digits, hyphens, and underscores"
        )
