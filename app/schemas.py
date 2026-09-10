"""Pydantic request/response DTOs for the public API."""
from datetime import datetime

from pydantic import BaseModel, Field

from app.config import settings


class ShortenRequest(BaseModel):
    original_url: str = Field(..., min_length=1, max_length=2048, description="The long URL to shorten")
    custom_alias: str | None = Field(
        default=None,
        min_length=settings.custom_alias_min_length,
        max_length=settings.custom_alias_max_length,
        description="Optional vanity alias instead of an auto-generated Base62 code",
    )
    expires_at: datetime | None = Field(default=None, description="Optional TTL as an absolute timestamp")


class ShortenResponse(BaseModel):
    short_code: str
    short_url: str
    original_url: str
    created_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class ErrorResponse(BaseModel):
    error: str
    message: str
    timestamp: datetime


class TimeBucket(BaseModel):
    bucket: datetime
    clicks: int


class ReferrerCount(BaseModel):
    referrer: str | None
    clicks: int


class AnalyticsResponse(BaseModel):
    short_code: str
    total_clicks: int
    time_series: list[TimeBucket]
    top_referrers: list[ReferrerCount]
