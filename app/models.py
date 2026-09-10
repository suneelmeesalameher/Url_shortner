"""SQLAlchemy ORM models (persistence layer schema)."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Sequence, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Explicit sequence (rather than a plain autoincrement column) so the repository layer
# can pull the next id via `nextval()` *before* the row is inserted. That id is what gets
# Base62-encoded into the short_code, letting us write short_code + all other columns in a
# single INSERT instead of an insert-then-update round trip.
url_id_seq = Sequence("url_id_seq")


class Url(Base):
    __tablename__ = "urls"

    id: Mapped[int] = mapped_column(
        BigInteger, url_id_seq, primary_key=True, server_default=url_id_seq.next_value()
    )
    short_code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    is_custom_alias: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    click_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class AnalyticsEvent(Base):
    """One row per click, written in batches by the async analytics worker (never on
    the redirect request path - see app.services.analytics_recorder).

    In a full deployment this table would be RANGE-partitioned by clicked_at (e.g.
    monthly) via a migration, since it grows far faster than `urls`; the ORM mapping
    itself doesn't need to know about that.
    """

    __tablename__ = "analytics_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    url_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("urls.id"), nullable=False, index=True)
    short_code: Mapped[str] = mapped_column(String(20), nullable=False)
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
