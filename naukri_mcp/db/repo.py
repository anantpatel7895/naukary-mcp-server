"""Query helpers. Keeps SQLAlchemy specifics out of the tool layer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from naukri_mcp.db.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    ApplyMode,
    CompanyBlocklist,
    EmailLog,
    Job,
    MasterProfileRow,
    ResumeVariant,
    utcnow,
)
from naukri_mcp.providers.base import JobPosting
from naukri_mcp.resume.profile import MasterProfile


# --- profile ---------------------------------------------------------------------


def save_profile(session: Session, profile: MasterProfile) -> MasterProfileRow:
    """Insert a new active profile, retiring any previous one."""
    for row in session.scalars(
        select(MasterProfileRow).where(MasterProfileRow.is_active.is_(True))
    ):
        row.is_active = False
    row = MasterProfileRow(
        is_active=True,
        source_file=profile.source_file,
        payload=profile.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    return row


def get_active_profile_row(session: Session) -> MasterProfileRow | None:
    return session.scalars(
        select(MasterProfileRow)
        .where(MasterProfileRow.is_active.is_(True))
        .order_by(MasterProfileRow.id.desc())
    ).first()


def get_active_profile(session: Session) -> tuple[int, MasterProfile] | None:
    row = get_active_profile_row(session)
    if row is None:
        return None
    return row.id, MasterProfile.model_validate(row.payload)


# --- jobs ------------------------------------------------------------------------


def upsert_job(session: Session, posting: JobPosting) -> Job:
    """Insert, or update in place if we have seen this provider job before."""
    existing = session.scalars(
        select(Job).where(
            Job.provider == posting.provider,
            Job.provider_job_id == posting.provider_job_id,
        )
    ).first()

    fields = dict(
        title=posting.title,
        company=posting.company,
        location=posting.location,
        url=posting.url,
        apply_url=posting.apply_url,
        contact_email=posting.contact_email,
        experience_min=posting.experience_min,
        experience_max=posting.experience_max,
        salary_text=posting.salary_text,
        posted_at=posting.posted_at,
        skills=posting.skills,
        raw=posting.raw,
    )

    if existing:
        for key, value in fields.items():
            setattr(existing, key, value)
        # A search hit carries a truncated description; never let it clobber a full JD.
        if len(posting.description) >= len(existing.description or ""):
            existing.description = posting.description
        session.flush()
        return existing

    job = Job(
        provider=posting.provider,
        provider_job_id=posting.provider_job_id,
        description=posting.description,
        **fields,
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: int) -> Job | None:
    return session.get(Job, job_id)


def is_blocked_company(session: Session, company: str) -> str | None:
    """Return the blocking reason if `company` matches a blocklist pattern."""
    if not company:
        return None
    needle = company.lower()
    for row in session.scalars(select(CompanyBlocklist)):
        if row.pattern.lower() in needle:
            return row.reason or row.pattern
    return None


# --- variants --------------------------------------------------------------------


def save_variant(
    session: Session,
    *,
    profile_id: int,
    job_id: int | None,
    label: str,
    docx_path: str,
    pdf_path: str,
    tailoring: dict,
    surfaced_keywords: list[str],
    score_before: float | None,
    score_after: float | None,
) -> ResumeVariant:
    variant = ResumeVariant(
        profile_id=profile_id,
        job_id=job_id,
        label=label,
        docx_path=docx_path,
        pdf_path=pdf_path,
        tailoring=tailoring,
        surfaced_keywords=surfaced_keywords,
        match_score_before=score_before,
        match_score_after=score_after,
    )
    session.add(variant)
    session.flush()
    return variant


def get_variant(session: Session, variant_id: int) -> ResumeVariant | None:
    return session.get(ResumeVariant, variant_id)


def latest_variant_for_job(session: Session, job_id: int) -> ResumeVariant | None:
    return session.scalars(
        select(ResumeVariant)
        .where(ResumeVariant.job_id == job_id)
        .order_by(ResumeVariant.id.desc())
    ).first()


# --- applications ----------------------------------------------------------------


def get_application_for_job(session: Session, job_id: int) -> Application | None:
    return session.scalars(select(Application).where(Application.job_id == job_id)).first()


def record_application(
    session: Session,
    *,
    job_id: int,
    variant_id: int | None,
    status: ApplicationStatus,
    mode: ApplyMode,
    notes: str = "",
) -> Application:
    """Create or update the single application row for this job."""
    app = get_application_for_job(session, job_id)
    if app is None:
        app = Application(job_id=job_id, variant_id=variant_id, status=status, mode=mode, notes=notes)
        session.add(app)
    else:
        app.status = status
        app.mode = mode
        if variant_id:
            app.variant_id = variant_id
        if notes:
            app.notes = notes
    if status == ApplicationStatus.applied and app.applied_at is None:
        app.applied_at = utcnow()
    session.flush()
    return app


def add_event(
    session: Session, application_id: int, kind: str, detail: str = "", data: dict | None = None
) -> ApplicationEvent:
    event = ApplicationEvent(
        application_id=application_id, kind=kind, detail=detail, data=data or {}
    )
    session.add(event)
    session.flush()
    return event


def list_applications(
    session: Session, status: ApplicationStatus | None = None, limit: int = 100
) -> list[Application]:
    stmt = select(Application).order_by(Application.updated_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Application.status == status)
    return list(session.scalars(stmt))


def stale_applications(session: Session, days: int = 7) -> list[Application]:
    """Applied, still unanswered, and older than `days` -- the follow-up queue."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(Application)
        .where(
            Application.status == ApplicationStatus.applied,
            Application.applied_at.is_not(None),
            Application.applied_at < cutoff,
        )
        .order_by(Application.applied_at.asc())
    )
    return list(session.scalars(stmt))


# --- email log -------------------------------------------------------------------


def log_email(
    session: Session,
    *,
    application_id: int | None,
    to_email: str,
    subject: str,
    body: str,
    attachments: list[str],
    dry_run: bool,
    sent: bool,
    error: str = "",
) -> EmailLog:
    row = EmailLog(
        application_id=application_id,
        to_email=to_email,
        subject=subject,
        body=body,
        attachments=attachments,
        dry_run=dry_run,
        sent=sent,
        error=error,
    )
    session.add(row)
    session.flush()
    return row


# --- analytics -------------------------------------------------------------------


def variant_performance(session: Session) -> list[dict]:
    """Callback rate per resume variant.

    The point of storing surfaced_keywords: over time this answers which keywords
    actually correlate with a reply, instead of guessing.
    """
    positive = {
        ApplicationStatus.screening,
        ApplicationStatus.interview,
        ApplicationStatus.offer,
    }
    rows: dict[int, dict] = {}
    for app in session.scalars(select(Application).where(Application.variant_id.is_not(None))):
        variant = session.get(ResumeVariant, app.variant_id)
        if variant is None:
            continue
        entry = rows.setdefault(
            variant.id,
            {
                "variant_id": variant.id,
                "label": variant.label,
                "surfaced_keywords": variant.surfaced_keywords,
                "applications": 0,
                "responses": 0,
            },
        )
        entry["applications"] += 1
        if app.status in positive:
            entry["responses"] += 1

    out = list(rows.values())
    for entry in out:
        entry["response_rate"] = (
            round(entry["responses"] / entry["applications"], 3) if entry["applications"] else 0.0
        )
    return sorted(out, key=lambda e: (-e["response_rate"], -e["applications"]))
