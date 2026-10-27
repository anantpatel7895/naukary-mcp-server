"""Parse a job description you paste in (or a URL we can fetch).

This is the path that always works: no API key, no scraping, no ToS question. It is
also the fallback whenever the Naukri adapter gets blocked, which is why the whole
tailoring pipeline is built on top of it rather than on top of a scraper.
"""

from __future__ import annotations

import hashlib
import re

import httpx

from naukri_mcp.http import build_ssl_context
from naukri_mcp.providers.base import (
    JobPosting,
    JobProvider,
    ProviderUnsupported,
    SearchQuery,
)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# "3-5 years", "3 to 5 yrs", "5+ years", "minimum 4 years"
EXPERIENCE_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I
)
EXPERIENCE_MIN_RE = re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)", re.I)
EXPERIENCE_SINGLE_RE = re.compile(
    r"(?:minimum|min\.?|at\s+least|over)\s*(\d{1,2})\s*(?:years?|yrs?)", re.I
)

SALARY_RE = re.compile(
    r"(?:₹|rs\.?|inr|\$)\s?[\d,.]+\s*(?:-|–|to)?\s*(?:₹|rs\.?|inr|\$)?\s?[\d,.]*\s*"
    r"(?:lpa|lakhs?|lac|cr|crore|per\s+annum|pa|k)?",
    re.I,
)

_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    "title": re.compile(r"^\s*(?:job\s+)?title\s*[:\-]\s*(.+)$", re.I | re.M),
    "company": re.compile(r"^\s*(?:company|organisation|organization|employer)\s*[:\-]\s*(.+)$", re.I | re.M),
    "location": re.compile(r"^\s*(?:location|place|city|work\s+location)\s*[:\-]\s*(.+)$", re.I | re.M),
}


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _extract_labeled(text: str, key: str) -> str:
    m = _LABEL_PATTERNS[key].search(text)
    return m.group(1).strip() if m else ""


def parse_experience(text: str) -> tuple[float | None, float | None]:
    if m := EXPERIENCE_RANGE_RE.search(text):
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo, hi) if lo <= hi else (hi, lo)
    if m := EXPERIENCE_MIN_RE.search(text):
        return float(m.group(1)), None
    if m := EXPERIENCE_SINGLE_RE.search(text):
        return float(m.group(1)), None
    return None, None


def parse_job_text(
    text: str,
    *,
    title: str = "",
    company: str = "",
    location: str = "",
    url: str = "",
    contact_email: str = "",
) -> JobPosting:
    """Build a JobPosting from raw JD text.

    Explicit arguments always win over anything sniffed out of the text -- the caller
    knows better than our regexes.
    """
    text = text.strip()
    if not text:
        raise ValueError("Job description text is empty.")

    resolved_title = title or _extract_labeled(text, "title") or _first_nonempty_line(text)
    resolved_company = company or _extract_labeled(text, "company")
    resolved_location = location or _extract_labeled(text, "location")

    email = contact_email
    if not email:
        found = EMAIL_RE.findall(text)
        email = found[0] if found else ""

    exp_min, exp_max = parse_experience(text)

    salary = ""
    if m := SALARY_RE.search(text):
        candidate = m.group(0).strip()
        # Guard against matching a bare currency symbol with no number.
        if any(ch.isdigit() for ch in candidate):
            salary = candidate

    # Stable id so re-pasting the same JD does not create a duplicate job row.
    digest = hashlib.sha256(
        f"{resolved_title}|{resolved_company}|{text}".encode()
    ).hexdigest()[:24]

    return JobPosting(
        provider="manual",
        provider_job_id=digest,
        title=resolved_title[:512],
        company=resolved_company[:512],
        location=resolved_location[:512],
        description=text,
        url=url,
        apply_url=url,
        contact_email=email,
        experience_min=exp_min,
        experience_max=exp_max,
        salary_text=salary[:256],
        raw={"source": "manual"},
    )


class ManualProvider(JobProvider):
    """Not a search source -- a parser. `search` is intentionally unsupported."""

    name = "manual"

    async def search(self, query: SearchQuery) -> list[JobPosting]:
        raise ProviderUnsupported(
            "The manual provider cannot search. Paste a job description into "
            "parse_job_description instead."
        )

    async def fetch_url(self, url: str, *, timeout: float = 20.0) -> JobPosting:
        """Fetch a JD page and parse its visible text.

        Best-effort: many job boards render client-side or gate on login, in which case
        the caller should paste the text instead.
        """
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            verify=build_ssl_context(),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = html_to_text(resp.text)
        if len(text) < 120:
            raise ValueError(
                f"Fetched {url} but only recovered {len(text)} characters of text -- the page "
                "is probably rendered client-side or behind a login. Paste the JD text instead."
            )
        return parse_job_text(text, url=url)


_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """Crude tag-strip. Good enough for JD pages; avoids a bs4/lxml dependency."""
    import html as html_mod

    text = _SCRIPT_STYLE_RE.sub(" ", html)
    # Preserve block boundaries so bullets do not run together.
    text = re.sub(r"<(br|/p|/div|/li|/tr|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS_RE.sub("\n\n", text).strip()
