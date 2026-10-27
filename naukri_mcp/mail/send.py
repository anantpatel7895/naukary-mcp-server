"""SMTP delivery for application emails.

Every attempt is written to `email_log` first, including dry runs, so there is always a
record of what the server intended to send even if the network call fails.
"""

from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from pydantic import BaseModel, Field

from naukri_mcp.config import Settings


class OutboundEmail(BaseModel):
    to_email: str
    subject: str
    body: str
    attachments: list[Path] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)


class SendResult(BaseModel):
    sent: bool
    dry_run: bool
    error: str = ""
    detail: str = ""


def build_message(email: OutboundEmail, settings: Settings) -> EmailMessage:
    msg = EmailMessage()
    from_display = (
        f"{settings.from_name} <{settings.from_email}>"
        if settings.from_name
        else settings.from_email
    )
    msg["From"] = from_display
    msg["To"] = email.to_email
    if email.cc:
        msg["Cc"] = ", ".join(email.cc)
    msg["Subject"] = email.subject
    msg.set_content(email.body)

    for path in email.attachments:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Attachment not found: {path}")
        ctype, encoding = mimetypes.guess_type(str(path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return msg


def send_email(email: OutboundEmail, settings: Settings) -> SendResult:
    """Send, or simulate sending when DRY_RUN is on.

    The caller is responsible for having obtained human confirmation first -- this
    function does not gate; it only refuses to act when DRY_RUN or config says so.
    """
    if not email.to_email:
        return SendResult(sent=False, dry_run=settings.dry_run, error="No recipient address.")

    try:
        message = build_message(email, settings)
    except FileNotFoundError as exc:
        return SendResult(sent=False, dry_run=settings.dry_run, error=str(exc))

    attachment_names = [Path(p).name for p in email.attachments]

    if settings.dry_run:
        return SendResult(
            sent=False,
            dry_run=True,
            detail=(
                f"DRY RUN -- nothing sent. Would email {email.to_email} "
                f"subject={email.subject!r} with {len(attachment_names)} attachment(s): "
                f"{', '.join(attachment_names) or 'none'}. "
                "Set DRY_RUN=false in your .env to send for real."
            ),
        )

    if not settings.smtp_configured:
        return SendResult(
            sent=False,
            dry_run=False,
            error=(
                "SMTP is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and "
                "FROM_EMAIL in your .env (use an app password, not your account password)."
            ),
        )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if settings.smtp_starttls:
                smtp.starttls()
                smtp.ehlo()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        return SendResult(
            sent=False,
            dry_run=False,
            error=(
                f"SMTP auth rejected ({exc.smtp_code}). For Gmail you need a 16-character "
                "app password with 2FA enabled, not your normal password."
            ),
        )
    except (smtplib.SMTPException, OSError) as exc:
        return SendResult(sent=False, dry_run=False, error=f"{type(exc).__name__}: {exc}")

    return SendResult(
        sent=True,
        dry_run=False,
        detail=f"Sent to {email.to_email} with {len(attachment_names)} attachment(s).",
    )


DEFAULT_SUBJECT = "Application for {title}{at_company} - {name}"

DEFAULT_BODY = """Dear Hiring Team,

I am writing to apply for the {title} position{at_company}. {pitch}

My resume is attached for your review. I would welcome the chance to discuss how my
experience fits what your team is building.

Thank you for your time and consideration.

Best regards,
{name}
{phone}{email_line}"""


def default_subject(*, title: str, company: str, name: str) -> str:
    return DEFAULT_SUBJECT.format(
        title=title or "the advertised role",
        at_company=f" at {company}" if company else "",
        name=name or "",
    ).strip(" -")


def default_body(
    *, title: str, company: str, name: str, phone: str = "", email: str = "", pitch: str = ""
) -> str:
    return DEFAULT_BODY.format(
        title=title or "advertised",
        at_company=f" at {company}" if company else "",
        pitch=pitch or "I believe my background is a strong match for the requirements.",
        name=name or "",
        phone=phone or "",
        email_line=f"\n{email}" if email else "",
    )
