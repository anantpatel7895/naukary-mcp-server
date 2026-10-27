"""SQLAlchemy 2.0 ORM models.

Deliberately small and denormalized where it helps: this is a single-user job-hunt
database, not a multi-tenant product.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class ApplicationStatus(str, enum.Enum):
    """The pipeline. `saved` means interesting-but-not-applied."""

    saved = "saved"
    applied = "applied"
    screening = "screening"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"
    no_response = "no_response"


class ApplyMode(str, enum.Enum):
    email = "email"
    assisted = "assisted"
    manual = "manual"


class MasterProfileRow(Base):
    """The parsed resume. Versioned by insert so re-importing never destroys history."""

    __tablename__ = "master_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source_file: Mapped[str] = mapped_column(String(1024), default="")
    #: Serialized resume.profile.MasterProfile
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    variants: Mapped[list[ResumeVariant]] = relationship(back_populates="profile")


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("provider", "provider_job_id", name="uq_job_provider_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    provider_job_id: Mapped[str] = mapped_column(String(256))

    title: Mapped[str] = mapped_column(String(512))
    company: Mapped[str] = mapped_column(String(512), default="", index=True)
    location: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(2048), default="")
    apply_url: Mapped[str] = mapped_column(String(2048), default="")
    contact_email: Mapped[str] = mapped_column(String(320), default="")
    experience_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    experience_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_text: Mapped[str] = mapped_column(String(256), default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    applications: Mapped[list[Application]] = relationship(back_populates="job")


class ResumeVariant(Base):
    """One rendered, JD-tailored resume. Keeps the tailoring decisions for auditability."""

    __tablename__ = "resume_variant"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    profile_id: Mapped[int] = mapped_column(ForeignKey("master_profile.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("job.id"), nullable=True)

    label: Mapped[str] = mapped_column(String(256), default="")
    docx_path: Mapped[str] = mapped_column(String(1024), default="")
    pdf_path: Mapped[str] = mapped_column(String(1024), default="")
    match_score_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_score_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: The approved tailored content plus every evidence_ref it cited.
    tailoring: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Keywords deliberately surfaced, so A/B analytics can attribute outcomes later.
    surfaced_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)

    profile: Mapped[MasterProfileRow] = relationship(back_populates="variants")


class Application(Base):
    """One application. The unique constraint on job_id is the duplicate-apply guard."""

    __tablename__ = "application"
    __table_args__ = (UniqueConstraint("job_id", name="uq_application_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    job_id: Mapped[int] = mapped_column(ForeignKey("job.id"), index=True)
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("resume_variant.id"), nullable=True)

    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus, native_enum=False), default=ApplicationStatus.saved, index=True
    )
    mode: Mapped[ApplyMode] = mapped_column(
        Enum(ApplyMode, native_enum=False), default=ApplyMode.manual
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    job: Mapped[Job] = relationship(back_populates="applications")
    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class ApplicationEvent(Base):
    """Append-only audit trail of everything that happened to an application."""

    __tablename__ = "application_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    application: Mapped[Application] = relationship(back_populates="events")


class EmailLog(Base):
    """Every send attempt, including dry runs. The record of what left the machine."""

    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("application.id"), nullable=True, index=True
    )
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(998), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")


class CompanyBlocklist(Base):
    __tablename__ = "company_blocklist"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    pattern: Mapped[str] = mapped_column(String(512), unique=True)
    reason: Mapped[str] = mapped_column(String(512), default="")


class SavedSearch(Base):
    __tablename__ = "saved_search"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    query: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result_count: Mapped[int] = mapped_column(Integer, default=0)
