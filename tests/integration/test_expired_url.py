"""Integration test: a short URL past its expires_at returns 410 Gone on redirect,
not a 404, a 500, or a silent redirect to a dead link.
"""
import time
import uuid
from datetime import datetime, timedelta, timezone


def test_expired_url_redirect_returns_410_gone(client):
    target_url = f"https://example.com/qa/expiring/{uuid.uuid4().hex}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=1)

    create_resp = client.post(
        "/api/v1/shorten",
        json={"original_url": target_url, "expires_at": expires_at.isoformat()},
    )
    assert create_resp.status_code == 201
    short_code = create_resp.json()["short_code"]

    # Confirm it's genuinely reachable before it expires.
    live_resp = client.get(f"/{short_code}", follow_redirects=False)
    assert live_resp.status_code == 302

    time.sleep(1.5)  # let the TTL actually elapse

    expired_resp = client.get(f"/{short_code}", follow_redirects=False)
    assert expired_resp.status_code == 410
    body = expired_resp.json()
    assert body["error"] == "GONE"

    # A second request should hit the tombstone cache path and still report 410,
    # not fall through to a different (incorrect) status.
    second_resp = client.get(f"/{short_code}", follow_redirects=False)
    assert second_resp.status_code == 410


def test_shorten_rejects_an_expires_at_already_in_the_past(client):
    target_url = f"https://example.com/qa/{uuid.uuid4().hex}"
    past = datetime.now(timezone.utc) - timedelta(hours=1)

    resp = client.post("/api/v1/shorten", json={"original_url": target_url, "expires_at": past.isoformat()})
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_URL"
