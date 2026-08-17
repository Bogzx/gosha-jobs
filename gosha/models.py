"""SQLAlchemy ORM models for the GOSHA job-matching engine."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# ---------------------------------------------------------------------------
# Helpers for JSON-encoded list columns (SQLite doesn't have native arrays)
# ---------------------------------------------------------------------------

class _JSONListMixin:
    """Convenience methods for models that store JSON-encoded lists."""

    @staticmethod
    def _load_json_list(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                return val
            # Single value parsed (e.g. a number) — wrap it
            return [str(val)] if val else []
        except (json.JSONDecodeError, TypeError):
            # Not valid JSON — treat as a plain string from old schema.
            # e.g. "software engineer" → ["software engineer"]
            raw = raw.strip()
            return [raw] if raw else []

    @staticmethod
    def _dump_json_list(items: list[str]) -> str:
        return json.dumps(items)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tier limits — used by the bot to gate features
# ---------------------------------------------------------------------------

TIER_LIMITS: dict[str, dict[str, int | bool]] = {
    "free": {
        "max_subscriptions": 5,
        "max_keywords_per_sub": 5,
        "max_locations_per_sub": 3,
        "max_applications": 10,           # tracked applications
        "cover_letters_per_month": 5,     # AI cover letter generations
        "scrape_now_cooldown": 300,       # 5 min
        "semantic_matching": True,
        "email_delivery": False,
        "webhook_delivery": False,
        "priority_delivery": False,       # free users get jobs after pro
    },
    "pro": {
        "max_subscriptions": 15,
        "max_keywords_per_sub": 10,
        "max_locations_per_sub": 10,
        "max_applications": 999,          # unlimited tracking
        "cover_letters_per_month": 999,   # unlimited
        "scrape_now_cooldown": 120,       # 2 min
        "semantic_matching": True,
        "email_delivery": True,
        "webhook_delivery": False,
        "priority_delivery": True,        # pro users get jobs first
    },
    "unlimited": {
        "max_subscriptions": 999,
        "max_keywords_per_sub": 50,
        "max_locations_per_sub": 50,
        "max_applications": 999,
        "cover_letters_per_month": 999,
        "scrape_now_cooldown": 60,        # 1 min
        "semantic_matching": True,
        "email_delivery": True,
        "webhook_delivery": True,
        "priority_delivery": True,
    },
}


def get_tier_limits(tier: str) -> dict[str, int | bool]:
    """Return limits for a given tier, defaulting to free."""
    return TIER_LIMITS.get(tier, TIER_LIMITS["free"])


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="free"
    )

    # Web platform profile (populated via Discord OAuth)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=_utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Whether the bot shares a guild with this user (can DM them)
    in_guild: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # float32 bytes of the user's CV embedding (see gosha/embeddings.py)
    cv_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    user_jobs: Mapped[list[UserJob]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def limits(self) -> dict[str, int | bool]:
        return get_tier_limits(self.tier)


# ---------------------------------------------------------------------------
# Job — first-class entity, deduplicated by URL
# ---------------------------------------------------------------------------

class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str] = mapped_column(String(256), nullable=False, default="Unknown")
    location: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # As quoted by the source: could be monthly RON, annual USD, hourly EUR.
    # Display these; never compare them (see the normalised pair below).
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    salary_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Gross RON per month — the one basis on which salaries from eJobs
    # (monthly RON), BestJobs (monthly EUR) and RemoteOK (annual USD) can
    # actually be filtered and sorted against each other. See gosha/salary.py.
    salary_monthly_min_ron: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_monthly_max_ron: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # indeed, linkedin, glassdoor
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    # Date the posting went live on the source board (when extractable)
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # float32 bytes of the job-text embedding (see gosha/embeddings.py)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Cross-board duplicate group: id of the canonical job (the canonical
    # row points at itself; NULL = not yet grouped / unique)
    dedup_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Dead-link detection bookkeeping
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user_jobs: Mapped[list[UserJob]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Job id={self.id} title={self.title!r} company={self.company!r}>"


# ---------------------------------------------------------------------------
# Subscription — rich query object
# ---------------------------------------------------------------------------

class Subscription(Base, _JSONListMixin):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )

    # Search criteria — stored as JSON-encoded lists for SQLite compatibility
    _keywords: Mapped[str] = mapped_column("keywords", Text, nullable=False)
    _locations: Mapped[str] = mapped_column("locations", Text, nullable=False)
    _excluded_keywords: Mapped[str] = mapped_column(
        "excluded_keywords", Text, nullable=False, default="[]"
    )
    _company_blacklist: Mapped[str] = mapped_column(
        "company_blacklist", Text, nullable=False, default="[]"
    )
    _boards: Mapped[str] = mapped_column(
        "boards", Text, nullable=False, default='["indeed","linkedin","glassdoor"]'
    )
    _experience_levels: Mapped[str] = mapped_column(
        "experience_levels", Text, nullable=False, default='["any"]'
    )

    # Scalar filters
    remote_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_age_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Web platform additions
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notify_discord: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")

    # --- Property accessors for JSON list columns ---

    @property
    def keywords(self) -> list[str]:
        return self._load_json_list(self._keywords)

    @keywords.setter
    def keywords(self, value: list[str]) -> None:
        self._keywords = self._dump_json_list(value)

    @property
    def locations(self) -> list[str]:
        return self._load_json_list(self._locations)

    @locations.setter
    def locations(self, value: list[str]) -> None:
        self._locations = self._dump_json_list(value)

    @property
    def excluded_keywords(self) -> list[str]:
        return self._load_json_list(self._excluded_keywords)

    @excluded_keywords.setter
    def excluded_keywords(self, value: list[str]) -> None:
        self._excluded_keywords = self._dump_json_list(value)

    @property
    def company_blacklist(self) -> list[str]:
        return self._load_json_list(self._company_blacklist)

    @company_blacklist.setter
    def company_blacklist(self, value: list[str]) -> None:
        self._company_blacklist = self._dump_json_list(value)

    @property
    def boards(self) -> list[str]:
        return self._load_json_list(self._boards)

    @boards.setter
    def boards(self, value: list[str]) -> None:
        self._boards = self._dump_json_list(value)

    @property
    def experience_levels(self) -> list[str]:
        return self._load_json_list(self._experience_levels)

    @experience_levels.setter
    def experience_levels(self, value: list[str]) -> None:
        self._experience_levels = self._dump_json_list(value)

    def __repr__(self) -> str:
        return (
            f"<Subscription id={self.id} keywords={self.keywords!r} "
            f"locations={self.locations!r}>"
        )


# ---------------------------------------------------------------------------
# UserJob — delivery tracking + feedback
# ---------------------------------------------------------------------------

class UserJob(Base):
    """Tracks which jobs have been delivered to which users, plus feedback."""

    __tablename__ = "user_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id"), nullable=False
    )
    subscription_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("subscriptions.id"), nullable=True
    )
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    feedback: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # "interested" | "not_relevant"
    feedback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="user_jobs")
    job: Mapped[Job] = relationship(back_populates="user_jobs")

    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_user_job_delivery"),
    )

    def __repr__(self) -> str:
        return (
            f"<UserJob user_id={self.user_id} job_id={self.job_id} "
            f"feedback={self.feedback!r}>"
        )


# ---------------------------------------------------------------------------
# Application — tracks where users have applied
# ---------------------------------------------------------------------------

APPLICATION_STATUSES = [
    "applied",        # Just submitted
    "phone_screen",   # Got a phone screen
    "interview",      # Interviewing
    "offer",          # Received an offer
    "rejected",       # Rejected at any stage
    "withdrawn",      # User withdrew
]


class Application(Base):
    """Tracks a user's application to a specific job through the hiring pipeline."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="applied"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Where the application was recorded from: 'web' (Apply click) or 'discord'
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="discord"
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    user: Mapped[User] = relationship(passive_deletes=True)
    job: Mapped[Job] = relationship(passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_user_application"),
    )

    def __repr__(self) -> str:
        return f"<Application user={self.user_id} job={self.job_id} status={self.status!r}>"


# ---------------------------------------------------------------------------
# CoverLetter — stores generated cover letters + tracks monthly usage
# ---------------------------------------------------------------------------


class Outbox(Base):
    """Queue of Discord messages the web app asks the bot to send.

    The API process has no gateway connection, so it enqueues rows here;
    the bot polls every ~30s, sends the DM, and stamps sent_at. Rows that
    fail 5 times are considered dead and surfaced in the admin dashboard.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(passive_deletes=True)

    @property
    def payload_dict(self) -> dict[str, Any]:
        try:
            value = json.loads(self.payload)
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def __repr__(self) -> str:
        return f"<Outbox id={self.id} kind={self.kind!r} user={self.user_id}>"


class CoverLetter(Base):
    """Stores an AI-generated cover letter for a specific user + job pair."""

    __tablename__ = "cover_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    user: Mapped[User] = relationship(passive_deletes=True)
    job: Mapped[Job] = relationship(passive_deletes=True)

    def __repr__(self) -> str:
        return f"<CoverLetter user={self.user_id} job={self.job_id}>"
