"""Integration tests for both rate-limit dimensions: IP-scoped (middleware, applies
globally) and shortcode-scoped (dependency, applies to the redirect route only).

Both tests monkeypatch the relevant limit down to a tiny number for their own
duration - the session-wide default is set very high (see conftest.py) so ordinary
functional tests never trip it by accident - and each uses a unique fake client IP
via the `unique_ip` fixture so they never share a Redis token-bucket key with any
other test in the suite.
"""
import uuid

from app.config import settings


def test_ip_rate_limit_returns_429_with_retry_after(client, monkeypatch, unique_ip):
    monkeypatch.setattr(settings, "rate_limit_per_ip_per_minute", 3)
    headers = {"X-Forwarded-For": unique_ip}

    # A nonexistent short code still passes through the IP middleware (which runs
    # ahead of routing) before resolving to a 404 - so it's a clean way to generate
    # traffic against the IP limit without needing a real link for every request.
    statuses = [client.get("/does-not-exist", headers=headers, follow_redirects=False).status_code for _ in range(5)]

    assert statuses[:3] == [404, 404, 404], f"first 3 requests should pass the limit, got {statuses}"
    assert 429 in statuses[3:], f"expected a 429 once the limit is exceeded, got {statuses}"

    rate_limited_resp = client.get("/does-not-exist", headers=headers, follow_redirects=False)
    assert rate_limited_resp.status_code == 429
    assert "Retry-After" in rate_limited_resp.headers
    assert rate_limited_resp.json()["error"] == "RATE_LIMITED"


def test_shortcode_rate_limit_returns_429_independent_of_ip(client, monkeypatch, unique_ip):
    monkeypatch.setattr(settings, "rate_limit_per_shortcode_per_minute", 2)

    create_resp = client.post(
        "/api/v1/shorten", json={"original_url": f"https://example.com/qa/{uuid.uuid4().hex}"}
    )
    short_code = create_resp.json()["short_code"]
    headers = {"X-Forwarded-For": unique_ip}

    statuses = [
        client.get(f"/{short_code}", headers=headers, follow_redirects=False).status_code for _ in range(4)
    ]

    assert statuses[:2] == [302, 302], f"first 2 requests should pass the limit, got {statuses}"
    assert 429 in statuses[2:], f"expected a 429 once the shortcode limit is exceeded, got {statuses}"
