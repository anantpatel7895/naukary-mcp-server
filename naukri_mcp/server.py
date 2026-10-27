"""Naukri MCP server.

Tool surface for job search, ATS resume tailoring, applying, and application tracking.

Design note: the server never rewrites your resume by itself. `analyze_job_match` returns
a structured gap report; the connected model writes the tailored bullets; `render_resume`
then validates every one against your real resume before anything is written to disk.
That keeps a human in the loop and makes fabrication a hard error rather than a risk.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from naukri_mcp.config import Settings, get_settings
from naukri_mcp.db import repo
from naukri_mcp.db.models import ApplicationStatus, ApplyMode
from naukri_mcp.db.session import Database
from naukri_mcp.mail.send import OutboundEmail, default_body, default_subject, send_email
from naukri_mcp.providers.base import JobPosting, JobProvider, ProviderBlocked, SearchQuery
from naukri_mcp.providers.manual import ManualProvider, parse_job_text
from naukri_mcp.providers.registry import enabled_providers
from naukri_mcp.resume.keywords import Bucket, build_gap_report
from naukri_mcp.resume.parse import parse_resume
from naukri_mcp.resume.profile import MasterProfile
from naukri_mcp.resume.render import (
    TailoredBullet,
    TailoringProposal,
    apply_proposal,
    render_docx,
    render_pdf,
    safe_filename,
    validate_tailoring,
)
from naukri_mcp.resume.score import score_match


#: stdout is the MCP protocol channel on stdio transport, so diagnostics go to stderr.
log = logging.getLogger("naukri_mcp")


@dataclass
class AppContext:
    settings: Settings
    db: Database
    manual: ManualProvider
    #: Search providers are built once per process. Each owns a pooled HTTP client, so
    #: constructing them per request would leak a socket pool on every search.
    providers: list[JobProvider]


#: The tool-facing context, parameterized with our lifespan type.
ToolContext = Context[AppContext, Any]

#: The SDK does not support Context injection for static resources (only for templated
#: ones), so `resume://master` cannot reach the lifespan the way tools do. The lifespan
#: publishes its Database here instead, which keeps the resource on the same engine and
#: connection pool rather than opening a second one on every read.
_shared_db: Database | None = None


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    global _shared_db

    settings = get_settings()
    settings.ensure_dirs()
    db = Database(settings)
    _shared_db = db
    ctx = AppContext(
        settings=settings,
        db=db,
        manual=ManualProvider(),
        providers=enabled_providers(settings),
    )
    try:
        yield ctx
    finally:
        for provider in ctx.providers:
            await provider.aclose()
        await ctx.manual.aclose()
        _shared_db = None
        db.dispose()


mcp: MCPServer[AppContext] = MCPServer(
    "naukri-mcp",
    title="Naukri Job Search & ATS Resume Tailoring",
    instructions=(
        "Job search, ATS resume tailoring, applying, and application tracking.\n\n"
        "Typical flow:\n"
        "  1. import_resume(path) -- once, to build the master profile.\n"
        "  2. search_jobs(...) or parse_job_description(text=...) to get a job_id.\n"
        "  3. analyze_job_match(job_id) -- returns the keyword gap report and ATS score.\n"
        "  4. Write tailored bullets yourself, citing an evidence_ref from step 3 for "
        "each one, then call render_resume(job_id, bullets=[...]).\n"
        "  5. apply_to_job(job_id) -- email or assisted apply, always after confirmation.\n\n"
        "Hard rule: never invent experience. Keywords the report puts in the 'missing' "
        "bucket are absent from the candidate's resume; render_resume will reject any "
        "bullet asserting them. Surface what is genuinely there instead."
    ),
    lifespan=lifespan,
)


def _app(ctx: ToolContext) -> AppContext:
    return ctx.request_context.lifespan_context


def _require_profile(app: AppContext) -> tuple[int, MasterProfile]:
    with app.db.session() as session:
        found = repo.get_active_profile(session)
    if found is None:
        raise ValueError(
            "No resume imported yet. Run import_resume with the path to your PDF or DOCX first."
        )
    return found


# --- resume ----------------------------------------------------------------------


@mcp.tool()
async def import_resume(path: str, ctx: ToolContext) -> dict[str, Any]:
    """Parse a resume (PDF/DOCX/TXT) into the master profile and store it.

    Run this once. Everything else reads the stored profile. Re-running replaces the
    active profile but keeps prior versions in the database.

    Args:
        path: Path to the resume file, e.g. ~/Downloads/Resume.pdf
    """
    app = _app(ctx)
    profile = parse_resume(path)
    if profile.is_empty():
        raise ValueError(
            f"Parsed {path} but found no usable sections. If it is a scanned image it "
            "needs OCR first."
        )
    with app.db.session() as session:
        row = repo.save_profile(session, profile)
        profile_id = row.id

    log.info(f"Imported resume from {path} as profile {profile_id}")
    return {
        "profile_id": profile_id,
        "name": profile.contact.name,
        "email": profile.contact.email,
        "counts": {
            "skills": len(profile.skills),
            "experience": len(profile.experience),
            "experience_bullets": sum(len(e.bullets) for e in profile.experience),
            "projects": len(profile.projects),
            "education": len(profile.education),
            "evidence_refs": len(profile.evidence_index()),
        },
        "experience": [
            {"ref": e.ref, "title": e.title, "company": e.company, "dates": f"{e.start} - {e.end}"}
            for e in profile.experience
        ],
        "note": "Check the counts look right. If a section is missing, the parser may "
        "have missed an unusual heading -- paste the section and I can fix the parser.",
    }


@mcp.resource("resume://master", mime_type="application/json")
def master_resume() -> str:
    """The parsed master profile, including every evidence ref you can cite."""
    db = _shared_db
    if db is None:
        raise RuntimeError("Server is not initialized yet -- no database available.")
    with db.session() as session:
        found = repo.get_active_profile(session)
    if found is None:
        return '{"error": "No resume imported. Run import_resume first."}'
    return found[1].model_dump_json(indent=2)


# --- jobs ------------------------------------------------------------------------


def _store_posting(app: AppContext, posting: JobPosting) -> dict[str, Any]:
    with app.db.session() as session:
        job = repo.upsert_job(session, posting)
        blocked = repo.is_blocked_company(session, job.company)
        existing = repo.get_application_for_job(session, job.id)
        return {
            "job_id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "url": job.url,
            "contact_email": job.contact_email,
            "experience": f"{job.experience_min or '?'}-{job.experience_max or '?'} yrs",
            "salary": job.salary_text,
            "description_chars": len(job.description or ""),
            "already_tracked": existing.status.value if existing else None,
            "company_blocked": blocked,
        }


@mcp.tool()
async def parse_job_description(
    ctx: ToolContext,
    text: str = "",
    url: str = "",
    title: str = "",
    company: str = "",
    location: str = "",
) -> dict[str, Any]:
    """Turn a job description into a tracked job you can tailor against.

    This is the most reliable way to add a job: paste the JD text. It needs no API key
    and no scraping, and it is the fallback whenever a job board blocks automated access.

    Args:
        text: The full job description text. Preferred.
        url: A JD page to fetch instead. Often fails on login-gated or JS-rendered pages.
        title: Overrides the detected job title.
        company: Overrides the detected company.
        location: Overrides the detected location.
    """
    app = _app(ctx)
    if not text and not url:
        raise ValueError("Provide either text (preferred) or url.")

    if text:
        posting = parse_job_text(
            text, title=title, company=company, location=location, url=url
        )
    else:
        posting = await app.manual.fetch_url(url)
        if title:
            posting.title = title
        if company:
            posting.company = company
        if location:
            posting.location = location

    result = _store_posting(app, posting)
    result["next"] = f"analyze_job_match(job_id={result['job_id']})"
    return result


@mcp.tool()
async def search_jobs(
    ctx: ToolContext,
    keywords: str,
    location: str = "",
    experience_years: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search configured job providers.

    Requires a provider to be enabled. The Naukri provider is opt-in (NAUKRI_ENABLED=true
    plus NAUKRI_COOKIE) because Naukri has no public API and blocks automated access; see
    the README. If no provider is available, paste a JD into parse_job_description instead.

    Args:
        keywords: Role or skill keywords, e.g. "python backend engineer".
        location: City or region filter.
        experience_years: Your years of experience, for relevance filtering.
        limit: Maximum results (1-100).
    """
    app = _app(ctx)
    providers = app.providers
    if not providers:
        return {
            "results": [],
            "providers_tried": [],
            "error": (
                "No search provider is enabled. Either set NAUKRI_ENABLED=true with a "
                "NAUKRI_COOKIE in your .env, or paste the job description into "
                "parse_job_description(text=...) which always works."
            ),
        }

    query = SearchQuery(
        keywords=keywords, location=location, experience_years=experience_years, limit=limit
    )
    results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    for provider in providers:
        try:
            postings = await provider.search(query)
        except ProviderBlocked as exc:
            errors[provider.name] = (
                f"blocked: {exc}. Paste the JD into parse_job_description instead."
            )
            log.warning(f"{provider.name} blocked: {exc}")
            continue
        except Exception as exc:  # a broken adapter must not take down the tool
            errors[provider.name] = f"{type(exc).__name__}: {exc}"
            log.error(f"{provider.name} failed: {exc}")
            continue
        for posting in postings:
            results.append(_store_posting(app, posting))

    return {
        "results": results,
        "count": len(results),
        "providers_tried": [p.name for p in providers],
        "errors": errors,
    }


@mcp.tool()
async def check_naukri_session(
    ctx: ToolContext, live: bool = False
) -> dict[str, Any]:
    """Diagnose the configured Naukri cookie/header without guessing.

    Use this right after pasting a fresh cookie. By default it only inspects what was
    loaded from the environment and makes no network request. Pass live=true to send one
    real minimal search and see whether Naukri actually accepts the session.

    See docs/naukri-session.md for how to copy the cookie out of your browser.

    Args:
        live: Send one real request to Naukri to verify the session end to end.
    """
    app = _app(ctx)
    from naukri_mcp.providers.naukri import NaukriProvider

    provider = next((p for p in app.providers if p.name == "naukri"), None)
    owned = provider is None
    if provider is None:
        # Build a throwaway instance so the tool still explains *why* it is disabled.
        provider = NaukriProvider(app.settings)

    try:
        report = await provider.probe() if live else provider.inspect_session()
    finally:
        if owned:
            await provider.aclose()

    report["docs"] = "docs/naukri-session.md"
    if report.get("problems"):
        report["next"] = (
            "Fix the problems above in your .env, then restart the MCP server so it "
            "reloads the values."
        )
    elif not live:
        report["next"] = "Looks well-formed. Run again with live=true to confirm Naukri accepts it."
    return report


@mcp.tool()
async def get_job(job_id: int, ctx: ToolContext) -> dict[str, Any]:
    """Fetch a stored job including its full description.

    Args:
        job_id: The id returned by search_jobs or parse_job_description.
    """
    app = _app(ctx)
    with app.db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            raise ValueError(f"No job with id {job_id}.")
        existing = repo.get_application_for_job(session, job_id)
        return {
            "job_id": job.id,
            "provider": job.provider,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "url": job.url,
            "apply_url": job.apply_url,
            "contact_email": job.contact_email,
            "salary": job.salary_text,
            "experience_min": job.experience_min,
            "experience_max": job.experience_max,
            "description": job.description,
            "status": existing.status.value if existing else None,
        }


# --- tailoring -------------------------------------------------------------------


@mcp.tool()
async def analyze_job_match(job_id: int, ctx: ToolContext) -> dict[str, Any]:
    """Compare your resume against a job and return the ATS keyword gap report.

    Read-only: this rewrites nothing. Use the output to write tailored bullets, then pass
    them to render_resume.

    Buckets, and what you may do with each:
      have_surfaced -- already prominent; leave alone.
      have_buried   -- you have it but it is not visible; promote it into a lead bullet.
      adjacent      -- you have a related skill only; frame honestly, never claim it.
      missing       -- absent from your resume. DO NOT put it on the resume. Learning
                       target only. render_resume rejects bullets asserting these.

    Args:
        job_id: The job to analyze against.
    """
    app = _app(ctx)
    _profile_id, profile = _require_profile(app)

    with app.db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            raise ValueError(f"No job with id {job_id}.")
        jd_text = job.description or ""
        job_title, job_company = job.title, job.company

    if not jd_text.strip():
        raise ValueError(
            f"Job {job_id} has no description stored. Re-add it with "
            "parse_job_description(text=...) including the full JD."
        )

    report = build_gap_report(profile, jd_text, job_title=job_title, job_company=job_company)
    score = score_match(report)

    def render(bucket: Bucket) -> list[dict[str, Any]]:
        out = []
        for f in report.by_bucket(bucket):
            entry: dict[str, Any] = {"keyword": f.keyword, "required": f.required}
            if f.evidence_refs:
                entry["evidence_refs"] = f.evidence_refs[:4]
                entry["evidence_text"] = f.evidence_text
            if f.related_have:
                entry["you_have_instead"] = f.related_have
            if f.implied_by:
                entry["implied_by"] = f.implied_by
                entry["safe_to_state"] = True
            if f.weak_claim:
                entry["weak_claim"] = True
            out.append(entry)
        return out

    return {
        "job": {"job_id": job_id, "title": job_title, "company": job_company},
        "score": {
            "current": score.score,
            "achievable_without_fabricating": score.potential_score,
            "headline": score.headline,
            "counts": score.counts,
            "required_gaps": score.required_missing,
        },
        "have_surfaced": render(Bucket.have_surfaced),
        "have_buried": render(Bucket.have_buried),
        "adjacent": render(Bucket.adjacent),
        "missing_do_not_add": render(Bucket.missing),
        "unknown_terms_for_review": report.unknown_terms,
        "how_to_tailor": (
            "Rewrite bullets from have_buried and have_surfaced so the JD's language "
            "appears naturally, and lead with the strongest match. Every bullet you "
            "propose must cite the evidence_ref of the resume line it rewrites. Do not "
            "mention anything under missing_do_not_add or claim an adjacent keyword as "
            "your own -- render_resume will reject the whole proposal if you do."
        ),
        "next": f"render_resume(job_id={job_id}, bullets=[...], summary='...')",
    }


@mcp.tool()
async def render_resume(
    ctx: ToolContext,
    job_id: int,
    bullets: list[dict[str, Any]],
    summary: str = "",
    skill_order: list[str] | None = None,
    label: str = "",
) -> dict[str, Any]:
    """Validate tailored content against your real resume, then render DOCX + PDF.

    Rejects the whole proposal (rendering nothing) if any bullet lacks a valid
    evidence_ref or asserts a keyword your resume does not support. That refusal is the
    point: a lower-scoring resume is recoverable, a false one is not.

    Args:
        job_id: The job this variant targets.
        bullets: List of {"evidence_ref": "exp.0.bullet.1", "text": "...",
            "surfaces": ["fastapi"]}. evidence_ref must come from analyze_job_match
            or resume://master.
        summary: Optional rewritten summary/objective.
        skill_order: Optional reordering of existing skills. May not introduce new ones.
        label: Optional name for this variant, for later A/B analytics.
    """
    app = _app(ctx)
    profile_id, profile = _require_profile(app)

    with app.db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            raise ValueError(f"No job with id {job_id}.")
        jd_text, job_title, job_company = job.description or "", job.title, job.company

    report = build_gap_report(profile, jd_text, job_title=job_title, job_company=job_company)
    before = score_match(report)

    proposal = TailoringProposal(
        summary=summary,
        bullets=[TailoredBullet.model_validate(b) for b in bullets],
        skill_order=skill_order or [],
        label=label,
    )

    validation = validate_tailoring(proposal, profile, report)
    if not validation.ok:
        return {
            "rendered": False,
            "rejected": True,
            "errors": [e.model_dump() for e in validation.errors],
            "why": (
                "Nothing was written. Every bullet must cite an evidence_ref that exists "
                "in your resume, and may not assert a keyword from missing_do_not_add or "
                "claim an adjacent keyword outright. Fix the flagged lines and retry."
            ),
        }

    tailored = apply_proposal(profile, proposal)
    after = score_match(
        build_gap_report(tailored, jd_text, job_title=job_title, job_company=job_company)
    )

    stem = safe_filename(
        profile.contact.name or "resume", job_company or "", job_title or "", suffix=""
    )
    docx_path = app.settings.renders_dir / f"{stem}.docx"
    pdf_path = app.settings.renders_dir / f"{stem}.pdf"
    render_docx(tailored, docx_path)
    render_pdf(tailored, pdf_path)

    with app.db.session() as session:
        variant = repo.save_variant(
            session,
            profile_id=profile_id,
            job_id=job_id,
            label=label or f"{job_company} {job_title}".strip(),
            docx_path=str(docx_path),
            pdf_path=str(pdf_path),
            tailoring=proposal.model_dump(mode="json"),
            surfaced_keywords=validation.surfaced_keywords,
            score_before=before.score,
            score_after=after.score,
        )
        variant_id = variant.id

    return {
        "rendered": True,
        "variant_id": variant_id,
        "docx": str(docx_path),
        "pdf": str(pdf_path),
        "score_before": before.score,
        "score_after": after.score,
        "surfaced_keywords": validation.surfaced_keywords,
        "note": "DOCX is the ATS submission format; PDF is for human readers.",
        "next": f"apply_to_job(job_id={job_id}, variant_id={variant_id})",
    }


@mcp.prompt()
def tailor_resume(job_id: int) -> str:
    """Guided ATS tailoring workflow for a job."""
    return (
        f"Tailor my resume for job {job_id}.\n\n"
        f"1. Call analyze_job_match(job_id={job_id}).\n"
        "2. For each keyword in have_buried and the weak_claim entries, rewrite the cited "
        "resume bullet so the job's own language appears naturally. Keep every claim true "
        "to the original bullet -- you are rewording and reprioritizing, not inventing.\n"
        "3. Show me the before/after for each bullet and wait for my approval.\n"
        f"4. After I approve, call render_resume(job_id={job_id}, bullets=[...]) with an "
        "evidence_ref on every bullet.\n"
        "5. Tell me which keywords remain genuinely missing, so I know the real gaps."
    )


# --- applying and tracking -------------------------------------------------------


@mcp.tool()
async def apply_to_job(
    ctx: ToolContext,
    job_id: int,
    variant_id: int | None = None,
    mode: Literal["email", "assisted"] = "assisted",
    to_email: str = "",
    subject: str = "",
    body: str = "",
    confirm_send: bool = False,
) -> dict[str, Any]:
    """Apply to a job and record it. Refuses to apply twice to the same job.

    mode="assisted" (default) records the application and hands you the apply URL and the
    tailored files -- you complete the submission. Nothing is sent.

    mode="email" drafts an application email. It sends only when confirm_send=true AND
    DRY_RUN=false; otherwise it returns the draft for review. Always show the user the
    draft and get explicit approval before setting confirm_send.

    Args:
        job_id: Job to apply to.
        variant_id: Tailored resume to attach. Defaults to the latest for this job.
        mode: "assisted" to record and hand off, or "email" to draft/send.
        to_email: Recipient. Defaults to the contact email found in the JD.
        subject: Overrides the generated subject.
        body: Overrides the generated body.
        confirm_send: Must be true to actually transmit. Set only after user approval.
    """
    app = _app(ctx)
    _profile_id, profile = _require_profile(app)

    with app.db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            raise ValueError(f"No job with id {job_id}.")

        existing = repo.get_application_for_job(session, job_id)
        if existing and existing.status != ApplicationStatus.saved:
            return {
                "applied": False,
                "duplicate": True,
                "message": (
                    f"You already applied to '{job.title}' at '{job.company}' on "
                    f"{existing.applied_at or existing.created_at} "
                    f"(status={existing.status.value}). Not applying again. Use "
                    f"update_application_status to change its status."
                ),
                "application_id": existing.id,
            }

        blocked = repo.is_blocked_company(session, job.company)
        if blocked:
            return {
                "applied": False,
                "blocked": True,
                "message": f"'{job.company}' is on your blocklist ({blocked}).",
            }

        variant = (
            repo.get_variant(session, variant_id)
            if variant_id
            else repo.latest_variant_for_job(session, job_id)
        )
        job_title, job_company = job.title, job.company
        job_apply_url, job_url = job.apply_url, job.url
        job_contact = job.contact_email
        variant_docx = variant.docx_path if variant else ""
        variant_pdf = variant.pdf_path if variant else ""
        resolved_variant_id = variant.id if variant else None

    if mode == "assisted":
        with app.db.session() as session:
            application = repo.record_application(
                session,
                job_id=job_id,
                variant_id=resolved_variant_id,
                status=ApplicationStatus.applied,
                mode=ApplyMode.assisted,
            )
            repo.add_event(
                session, application.id, "assisted_apply", "Recorded; user submits manually."
            )
            application_id = application.id
        return {
            "applied": True,
            "mode": "assisted",
            "application_id": application_id,
            "apply_url": job_apply_url or job_url,
            "attach_docx": variant_docx,
            "attach_pdf": variant_pdf,
            "message": (
                "Recorded as applied. Open the apply URL and upload the DOCX. "
                if variant_docx
                else "Recorded as applied. No tailored resume yet -- run render_resume first "
                "if you want one."
            ),
        }

    # --- email mode ---
    recipient = to_email or job_contact
    if not recipient:
        return {
            "applied": False,
            "message": (
                "No recipient email. The JD did not contain one -- pass to_email, or use "
                "mode='assisted' to apply through the job board instead."
            ),
        }

    if not variant_docx:
        return {
            "applied": False,
            "message": (
                "No tailored resume to attach. Run analyze_job_match then render_resume "
                "first, or pass variant_id explicitly."
            ),
        }

    final_subject = subject or default_subject(
        title=job_title, company=job_company, name=profile.contact.name
    )
    final_body = body or default_body(
        title=job_title,
        company=job_company,
        name=profile.contact.name,
        phone=profile.contact.phone,
        email=profile.contact.email,
    )
    attachments = [Path(variant_docx)]

    if not confirm_send:
        return {
            "applied": False,
            "awaiting_confirmation": True,
            "draft": {
                "to": recipient,
                "subject": final_subject,
                "body": final_body,
                "attachments": [p.name for p in attachments],
            },
            "message": (
                "Draft only -- nothing sent. Show this to the user. If they approve, call "
                "again with confirm_send=true."
                + ("" if not app.settings.dry_run else " Note: DRY_RUN is on, so even with "
                   "confirm_send=true nothing will actually transmit until you set "
                   "DRY_RUN=false in .env.")
            ),
        }

    outbound = OutboundEmail(
        to_email=recipient, subject=final_subject, body=final_body, attachments=attachments
    )
    result = send_email(outbound, app.settings)

    with app.db.session() as session:
        application = repo.record_application(
            session,
            job_id=job_id,
            variant_id=resolved_variant_id,
            status=ApplicationStatus.applied if result.sent else ApplicationStatus.saved,
            mode=ApplyMode.email,
        )
        repo.log_email(
            session,
            application_id=application.id,
            to_email=recipient,
            subject=final_subject,
            body=final_body,
            attachments=[str(p) for p in attachments],
            dry_run=result.dry_run,
            sent=result.sent,
            error=result.error,
        )
        repo.add_event(
            session,
            application.id,
            "email_sent" if result.sent else "email_not_sent",
            result.detail or result.error,
        )
        application_id = application.id

    return {
        "applied": result.sent,
        "dry_run": result.dry_run,
        "application_id": application_id,
        "to": recipient,
        "error": result.error,
        "message": result.detail or result.error,
    }


@mcp.tool()
async def list_applications(
    ctx: ToolContext, status: str = "", limit: int = 50
) -> dict[str, Any]:
    """List tracked applications, newest first.

    Args:
        status: Optional filter -- saved, applied, screening, interview, offer, rejected,
            withdrawn, no_response.
        limit: Maximum rows.
    """
    app = _app(ctx)
    status_enum = None
    if status:
        try:
            status_enum = ApplicationStatus(status)
        except ValueError:
            raise ValueError(
                f"Unknown status {status!r}. Valid: "
                f"{', '.join(s.value for s in ApplicationStatus)}"
            ) from None

    with app.db.session() as session:
        rows = repo.list_applications(session, status_enum, limit)
        out = []
        for a in rows:
            job = repo.get_job(session, a.job_id)
            out.append(
                {
                    "application_id": a.id,
                    "job_id": a.job_id,
                    "title": job.title if job else "?",
                    "company": job.company if job else "?",
                    "status": a.status.value,
                    "mode": a.mode.value,
                    "applied_at": a.applied_at.isoformat() if a.applied_at else None,
                    "notes": a.notes,
                }
            )
        stale = [
            {"application_id": a.id, "job_id": a.job_id, "applied_at": a.applied_at.isoformat()}
            for a in repo.stale_applications(session, days=7)
        ]
    return {"applications": out, "count": len(out), "needs_followup_7d": stale}


@mcp.tool()
async def update_application_status(
    ctx: ToolContext, application_id: int, status: str, note: str = ""
) -> dict[str, Any]:
    """Move an application along the pipeline.

    Args:
        application_id: The application to update.
        status: saved, applied, screening, interview, offer, rejected, withdrawn,
            no_response.
        note: Optional note recorded on the audit trail.
    """
    app = _app(ctx)
    try:
        status_enum = ApplicationStatus(status)
    except ValueError:
        raise ValueError(
            f"Unknown status {status!r}. Valid: "
            f"{', '.join(s.value for s in ApplicationStatus)}"
        ) from None

    with app.db.session() as session:
        from naukri_mcp.db.models import Application

        application = session.get(Application, application_id)
        if application is None:
            raise ValueError(f"No application with id {application_id}.")
        previous = application.status.value
        application.status = status_enum
        if note:
            application.notes = note
        repo.add_event(
            session, application.id, "status_change", f"{previous} -> {status_enum.value}: {note}"
        )
    return {"application_id": application_id, "from": previous, "to": status_enum.value}


@mcp.tool()
async def variant_performance(ctx: ToolContext) -> dict[str, Any]:
    """Response rate per tailored resume variant.

    Tells you which tailoring choices actually produce replies, instead of guessing.
    Meaningful only once you have a dozen or so applications with outcomes recorded.
    """
    app = _app(ctx)
    with app.db.session() as session:
        rows = repo.variant_performance(session)
    return {
        "variants": rows,
        "note": "Low sample sizes are noise. Read this after ~20 applications.",
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
