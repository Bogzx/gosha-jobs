"""Pydantic schemas mirroring the API contracts in the design spec."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MeOut(BaseModel):
    id: int
    discord_id: str
    username: str | None
    avatar_url: str | None
    tier: str
    in_guild: bool
    has_cv: bool
    is_admin: bool


class JobSummaryOut(BaseModel):
    """Compact job payload embedded in applications."""

    id: int
    title: str
    company: str
    location: str
    url: str
    source: str


class JobOut(BaseModel):
    id: int
    url: str
    title: str
    company: str
    location: str
    description: str | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    source: str
    posted_at: datetime | None
    first_seen_at: datetime | None
    # Raw cosine, kept for debugging and API consumers.
    match_score: float | None = None
    # 0-100 position within the ranked candidate set. This is what the UI
    # renders: raw cosine sits in a narrow band and reads as a meaningless
    # low percentage.
    match_percentile: int | None = None
    match_reasons: list[str] | None = None
    feedback: str | None = None
    applied: bool = False


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    per_page: int


class FeedbackIn(BaseModel):
    feedback: str = Field(pattern="^(interested|not_relevant)$")


class ApplicationOut(BaseModel):
    id: int
    job_id: int
    status: str
    notes: str | None
    applied_at: datetime | None
    updated_at: datetime | None
    source: str
    job: JobSummaryOut


class ApplicationCreateIn(BaseModel):
    job_id: int


class ApplicationPatchIn(BaseModel):
    status: str | None = None
    notes: str | None = None


class SubscriptionIn(BaseModel):
    name: str | None = None
    keywords: list[str] = Field(min_length=1)
    locations: list[str] = Field(min_length=1)
    excluded_keywords: list[str] = []
    company_blacklist: list[str] = []
    experience_levels: list[str] = ["any"]
    remote_ok: bool = False
    salary_min: int | None = None
    max_age_days: int = Field(default=7, ge=1, le=30)
    notify_discord: bool = True


class SubscriptionPatchIn(BaseModel):
    name: str | None = None
    keywords: list[str] | None = None
    locations: list[str] | None = None
    excluded_keywords: list[str] | None = None
    company_blacklist: list[str] | None = None
    experience_levels: list[str] | None = None
    remote_ok: bool | None = None
    salary_min: int | None = None
    max_age_days: int | None = Field(default=None, ge=1, le=30)
    notify_discord: bool | None = None


class SubscriptionOut(BaseModel):
    id: int
    name: str | None
    keywords: list[str]
    locations: list[str]
    excluded_keywords: list[str]
    company_blacklist: list[str]
    experience_levels: list[str]
    remote_ok: bool
    salary_min: int | None
    max_age_days: int
    is_active: bool
    notify_discord: bool
    created_at: datetime | None


class PageviewIn(BaseModel):
    path: str = Field(max_length=512)


class OkOut(BaseModel):
    ok: bool = True
