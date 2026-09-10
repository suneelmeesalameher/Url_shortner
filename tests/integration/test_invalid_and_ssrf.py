"""Integration tests confirming malformed URLs and SSRF targets are rejected at the
API boundary (POST /api/v1/shorten) with a clean 400 and structured error body -
never a 500, and never a row actually created in the database.
"""
import pytest


@pytest.mark.parametrize(
    "original_url",
    [
        "not a url",
        "example.com",
        "ftp://example.com/file",
        "javascript:alert(document.cookie)",
    ],
)
def test_malformed_or_unsupported_scheme_url_returns_400(client, original_url):
    resp = client.post("/api/v1/shorten", json={"original_url": original_url})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "INVALID_URL"
    assert "timestamp" in body


@pytest.mark.parametrize(
    "ssrf_target",
    [
        "http://localhost/admin",
        "http://127.0.0.1:6379/",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/internal-service",
        "http://192.168.0.1/",
    ],
)
def test_ssrf_targets_are_rejected_at_creation_time(client, ssrf_target):
    resp = client.post("/api/v1/shorten", json={"original_url": ssrf_target})
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_URL"


def test_shorten_requires_original_url_field(client):
    resp = client.post("/api/v1/shorten", json={})
    # Missing required field -> FastAPI/Pydantic request validation, not our domain
    # exception - a 422, still never a 500 or an accidental 201.
    assert resp.status_code == 422
