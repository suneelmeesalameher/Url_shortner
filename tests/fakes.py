"""Shared test doubles used across unit tests.

`FakeUrlRepository` satisfies `UrlRepositoryPort` (app.core.ports) purely by
matching its method signatures - `Protocol` is structural, so no inheritance is
needed. Backed by a plain dict; no SQL, no network. This is what lets the unit
tests exercise `UrlService`'s actual business logic (validation, cache-aside
orchestration, expiration rules) without a database.
"""
from datetime import datetime, timezone

from app.models import Url


class FakeUrlRepository:
    def __init__(self):
        self.rows: dict[str, Url] = {}
        self._next_id = 1
        self.get_by_short_code_call_count = 0

    async def get_next_id(self, db):
        value = self._next_id
        self._next_id += 1
        return value

    async def create_url(self, db, *, url_id, short_code, original_url, expires_at, is_custom_alias):
        row = Url(
            id=url_id,
            short_code=short_code,
            original_url=original_url,
            expires_at=expires_at,
            is_custom_alias=is_custom_alias,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            click_count=0,
        )
        self.rows[short_code] = row
        return row

    async def get_by_short_code(self, db, short_code):
        self.get_by_short_code_call_count += 1
        return self.rows.get(short_code)
