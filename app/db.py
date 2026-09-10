"""Async SQLAlchemy engine/session setup (data layer plumbing)."""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import settings

# MODIFIED (test infrastructure): asyncpg connections are bound to the event loop
# that created them, but the test suite legitimately runs work across more than one
# loop in the same process (a sync `TestClient` drives its own internal loop, while
# a handful of tests use an async `httpx.AsyncClient` directly to get genuine
# concurrency - see tests/integration/test_custom_alias_race.py). With normal
# pooling, a connection checked out on one loop and left idle in the pool would be
# handed to a request running on a different loop and fail. `database_use_null_pool`
# (set only by tests/conftest.py, never in production) makes every checkout open a
# fresh connection and every checkin close it - no connection is ever reused across
# a loop boundary. Production is unaffected: the flag defaults to False and keeps
# the normal pooled engine.
_engine_kwargs: dict = {"pool_pre_ping": True}
if settings.database_use_null_pool:
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs["pool_size"] = 20
    _engine_kwargs["max_overflow"] = 10

engine = create_async_engine(settings.database_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # let us keep using ORM objects after commit (e.g. to build response DTOs)
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped DB session."""
    async with AsyncSessionLocal() as session:
        yield session
