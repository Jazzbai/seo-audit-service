"""SQLAlchemy models for the ForgeSEO foundation.

The models intentionally contain no application-specific runtime imports. All
database timestamps are naive UTC values; API layers are responsible for
serializing them as ISO timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    CheckConstraint,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return a timezone-naive UTC datetime for database storage."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def uuid_hex() -> str:
    """Return the opaque identifier format used by the application."""

    return uuid4().hex


def _dict() -> dict[str, Any]:
    return {}


def _list() -> list[Any]:
    return []


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_memberships_team_user"),
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="ck_memberships_role"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    team_id: Mapped[str] = mapped_column(String(32), ForeignKey("teams.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_expires", "user_id", "expires_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    # Bind each browser session to the team selected at authentication time.
    # Nullable keeps the migration compatible with pre-team-bound sessions;
    # auth rejects those legacy rows rather than guessing a membership.
    team_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("teams.id"), nullable=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (Index("ix_sites_team_created", "team_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    team_id: Mapped[str] = mapped_column(String(32), ForeignKey("teams.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    origin: Mapped[str] = mapped_column(String(2048), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Chicago")
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    paused: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("site_id", "kind", name="uq_connections_site_kind"),
        Index("ix_connections_site_status", "site_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="needs_connection")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("site_id", "version", name="uq_policies_site_version"),
        Index("ix_policies_site_version", "site_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    created_by: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("site_id", "resource_key", name="uq_pages_site_resource"),
        Index("ix_pages_site_enrolled", "site_id", "enrolled"),
        Index("ix_pages_site_last_seen", "site_id", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    resource_key: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, default="posts")
    source: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    signals: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enrolled: Mapped[bool] = mapped_column(nullable=False, default=False)
    managed: Mapped[bool] = mapped_column(nullable=False, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("site_id", "key", name="uq_findings_site_key"),
        Index("ix_findings_site_status_seen", "site_id", "status", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    page_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("pages.id"), nullable=True)
    key: Mapped[str] = mapped_column(String(512), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (Index("ix_candidates_site_status_created", "site_id", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    page_id: Mapped[str] = mapped_column(String(32), ForeignKey("pages.id"), nullable=False, index=True)
    field: Mapped[str] = mapped_column(String(128), nullable=False)
    before_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (Index("ix_articles_site_status_updated", "site_id", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    slug: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    brief: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    sources: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=_list)
    author_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remote_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    managed: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (Index("ix_revisions_article_created", "article_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    article_id: Mapped[str] = mapped_column(String(32), ForeignKey("articles.id"), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_publications_operation_key"),
        Index("ix_publications_site_status_updated", "site_id", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    article_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("articles.id"), nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("candidates.id"), nullable=True)
    operation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    remote_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        Index("ix_jobs_site_status_available", "site_id", "status", "available_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("site_id", "key", name="uq_incidents_site_key"),
        Index("ix_incidents_site_status_seen", "site_id", "status", "last_seen_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (Index("ix_measurements_site_kind_observed", "site_id", "kind", "observed_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class BudgetAccount(Base):
    __tablename__ = "budget_accounts"
    __table_args__ = (
        UniqueConstraint("site_id", "period", name="uq_budget_accounts_site_period"),
        Index("ix_budget_accounts_site_period", "site_id", "period"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    limit_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=30000)
    reserved_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spent_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CostReservation(Base):
    __tablename__ = "cost_reservations"
    __table_args__ = (Index("ix_cost_reservations_site_status", "site_id", "status"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_hex)
    site_id: Mapped[str] = mapped_column(String(32), ForeignKey("sites.id"), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(32), ForeignKey("budget_accounts.id"), nullable=False, index=True)
    operation_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    estimated_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_site_created", "site_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("sites.id"), nullable=True)
    team_id: Mapped[str] = mapped_column(String(32), ForeignKey("teams.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=_dict)


@event.listens_for(User, "before_insert")
@event.listens_for(User, "before_update")
def _normalize_user_email(mapper: Any, connection: Any, target: User) -> None:
    if isinstance(target.email, str):
        target.email = target.email.strip().casefold()


# Policies are versioned records. ORM-level guards prevent accidental in-place
# edits/deletes even when a caller bypasses policies.create_policy(). Database
# migrations retain the same append-only intent for application-facing writes.
@event.listens_for(Policy, "before_update")
def _reject_policy_update(mapper: Any, connection: Any, target: Policy) -> None:
    raise ValueError("policies are append-only")


@event.listens_for(Policy, "before_delete")
def _reject_policy_delete(mapper: Any, connection: Any, target: Policy) -> None:
    raise ValueError("policies are append-only")


__all__ = [
    "Article",
    "Base",
    "BudgetAccount",
    "Candidate",
    "Connection",
    "CostReservation",
    "Event",
    "Finding",
    "Heartbeat",
    "Incident",
    "Job",
    "Measurement",
    "Membership",
    "Page",
    "Policy",
    "Publication",
    "Revision",
    "Session",
    "Site",
    "Team",
    "User",
    "utcnow",
    "uuid_hex",
]
