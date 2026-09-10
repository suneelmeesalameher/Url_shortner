"""Integration test for the custom-alias race condition: many concurrent requests
for the SAME alias must result in exactly one winner, with the rest getting a clean
409 rather than a duplicate row, a crash, or a hung request.

Uses httpx.AsyncClient + asyncio.gather (rather than the session's sync TestClient
+ threads) to get genuine interleaved concurrency within one event loop against the
real Postgres unique constraint on `urls.short_code` - this is what actually proves
PostgresUrlRepository.create_url's IntegrityError -> DuplicateAliasError handling
is race-safe, as opposed to merely "not obviously broken" under sequential calls.
"""
import asyncio
import uuid

import httpx
import pytest

from app.main import app


@pytest.mark.usefixtures("db_schema", "redis_server")
async def test_concurrent_requests_for_same_custom_alias_have_exactly_one_winner():
    alias = f"race-{uuid.uuid4().hex[:10]}"
    concurrency = 15

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as ac:
        responses = await asyncio.gather(
            *[
                ac.post(
                    "/api/v1/shorten",
                    json={"original_url": f"https://example.com/race/{i}", "custom_alias": alias},
                )
                for i in range(concurrency)
            ]
        )

    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1, f"expected exactly one 201, got statuses={statuses}"
    assert statuses.count(409) == concurrency - 1, f"expected the rest to be 409, got statuses={statuses}"

    for resp in responses:
        if resp.status_code == 409:
            assert resp.json()["error"] == "ALIAS_TAKEN"

    winner = next(r for r in responses if r.status_code == 201)
    assert winner.json()["short_code"] == alias
