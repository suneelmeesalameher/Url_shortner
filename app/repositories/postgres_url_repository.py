"""Postgres implementation of `UrlRepositoryPort` (app.core.ports).

REFACTORED from the previous `url_repository.py` module-level functions into a class
for two reasons:
  1. It's what actually satisfies `UrlRepositoryPort` as a substitutable type - a
     module isn't a value you can pass around and swap for another implementation
     (e.g. a future `DynamoDbUrlRepository`); an instance of a class is. This is the
     Strategy pattern / Open-Closed half of the refactor: adding a DynamoDB backend
     means writing a new class with these same three methods and handing it to
     `UrlService` at the composition root (app.api.dependencies) - zero changes here
     or in the service.
  2. Defensive exception isolation (new in this pass): every method now wraps its
     `AsyncSession` calls in a narrow try/except. `IntegrityError` (a business
     outcome - the alias is taken) is still translated to `DuplicateAliasError` as
     before; anything else from SQLAlchemy (`OperationalError` for a dropped
     connection, timeouts, etc.) is now caught and re-raised as
     `StorageUnavailableError` instead of propagating as a raw driver exception that
     would otherwise surface to the client as an unstructured 500.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DuplicateAliasError, StorageUnavailableError
from app.models import Url, url_id_seq


class PostgresUrlRepository:
    """Stateless adapter - safe to construct once as a process-lifetime singleton
    (see app.api.dependencies) since it holds no per-request state itself; the
    request-scoped `AsyncSession` is passed into every call.
    """

    async def get_next_id(self, db: AsyncSession) -> int:
        """Pull the next value from the id sequence without inserting a row.

        This is what lets the service layer Base62-encode the id *before* the row
        exists, so short_code can be written in the same INSERT as everything else.
        """
        try:
            result = await db.execute(select(url_id_seq.next_value()))
            return result.scalar_one()
        except SQLAlchemyError as exc:
            raise StorageUnavailableError("get_next_id", exc) from exc

    async def create_url(
        self,
        db: AsyncSession,
        *,
        url_id: int,
        short_code: str,
        original_url: str,
        expires_at: datetime | None,
        is_custom_alias: bool,
    ) -> Url:
        """Insert a new short URL row. Raises DuplicateAliasError on a unique-constraint
        violation, StorageUnavailableError on any other DB-level failure.
        """
        new_url = Url(
            id=url_id,
            short_code=short_code,
            original_url=original_url,
            expires_at=expires_at,
            is_custom_alias=is_custom_alias,
        )
        db.add(new_url)
        try:
            await db.commit()
        except IntegrityError as exc:
            # The unique constraint on short_code is the only one that can fire here -
            # translate the low-level DB error into an explicit domain exception.
            await db.rollback()
            raise DuplicateAliasError(short_code) from exc
        except SQLAlchemyError as exc:
            await db.rollback()
            raise StorageUnavailableError("create_url", exc) from exc

        await db.refresh(new_url)
        return new_url

    async def get_by_short_code(self, db: AsyncSession, short_code: str) -> Url | None:
        try:
            result = await db.execute(select(Url).where(Url.short_code == short_code))
            return result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            raise StorageUnavailableError("get_by_short_code", exc) from exc
