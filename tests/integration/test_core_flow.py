"""Integration test for the primary user journey, against a real Postgres + Redis:

POST /api/v1/shorten -> GET /{shortCode} (302 redirect) -> analytics counter updated.

This is the one test that exercises the full stack end-to-end: DB id generation,
Base62 encoding, cache population, the 302 redirect contract, the async click-event
queue, the background analytics worker flush, and the analytics read endpoint.
"""
import time
import uuid


def test_shorten_redirect_and_analytics_full_flow(client):
    target_url = f"https://example.com/qa/{uuid.uuid4().hex}"

    create_resp = client.post("/api/v1/shorten", json={"original_url": target_url})
    assert create_resp.status_code == 201
    body = create_resp.json()
    assert body["original_url"] == target_url
    assert body["short_url"].endswith(f"/{body['short_code']}")
    short_code = body["short_code"]

    # First redirect: cache miss -> DB lookup -> cache populated.
    redirect_resp = client.get(f"/{short_code}", follow_redirects=False)
    assert redirect_resp.status_code == 302
    assert redirect_resp.headers["location"] == target_url
    assert redirect_resp.headers["cache-control"] == "no-cache, no-store, must-revalidate"

    # Second redirect: should now be served from cache, same result either way.
    redirect_resp_2 = client.get(f"/{short_code}", follow_redirects=False)
    assert redirect_resp_2.status_code == 302
    assert redirect_resp_2.headers["location"] == target_url

    # The analytics worker flushes on a short interval in tests (see conftest) -
    # give it a moment to batch-write both clicks before reading them back.
    _wait_until(lambda: _get_total_clicks(client, short_code) == 2, timeout=3.0)

    analytics_resp = client.get(f"/api/v1/urls/{short_code}/analytics")
    assert analytics_resp.status_code == 200
    analytics = analytics_resp.json()
    assert analytics["short_code"] == short_code
    assert analytics["total_clicks"] == 2
    assert sum(bucket["clicks"] for bucket in analytics["time_series"]) == 2


def _get_total_clicks(client, short_code: str) -> int:
    resp = client.get(f"/api/v1/urls/{short_code}/analytics")
    return resp.json()["total_clicks"] if resp.status_code == 200 else -1


def _wait_until(predicate, timeout: float, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"Condition not met within {timeout}s")
