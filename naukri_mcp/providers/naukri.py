"""Naukri adapter -- opt-in, fragile by nature.

Naukri publishes no public job-search API, its Terms prohibit automated access, and its
robots.txt (edited 2026-05-13) sets `Disallow: /` for AI agents while allowing only
/blog/, /career-advice/, /code360/, /recruit/ and /resume-maker. This adapter therefore:

  * is off unless NAUKRI_ENABLED=true and a NAUKRI_COOKIE is supplied,
  * uses *your own* logged-in session rather than crawling anonymously,
  * runs at roughly one request every three seconds, single-user volume,
  * caches aggressively so repeated analysis never re-requests,
  * and raises ProviderBlocked (not a generic error) the moment it is refused, so the
    caller can tell you to paste the JD into parse_job_description instead.

Expect it to break without notice. Everything else in this server is built to work
without it -- that is deliberate, not incidental.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from naukri_mcp.config import Settings
from naukri_mcp.http import build_ssl_context
from naukri_mcp.providers.base import (
    JobPosting,
    JobProvider,
    ProviderBlocked,
    ProviderError,
    SearchQuery,
)

SEARCH_URL = "https://www.naukri.com/jobapi/v3/search"
DETAIL_URL = "https://www.naukri.com/jobapi/v4/job/{job_id}"

# Naukri's web client identifies itself with these; without them the endpoint 400s.
BASE_HEADERS: dict[str, str] = {
    "appid": "109",
    "systemid": "Naukri",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.naukri.com/",
}


#: Cookie names Naukri uses to carry a logged-in session. Used only as a sanity hint
#: when diagnosing a paste -- never required, since Naukri may rename them at any time.
_AUTH_COOKIE_HINTS = {"nauk_at", "nauk_rt", "nkwap", "ninjaid", "_t_us", "_t_ut", "ps"}


class RateLimiter:
    """Token-bucket-free minimum-interval limiter. Simple and adequate for one user."""

    def __init__(self, rps: float) -> None:
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            gap = now - self._last
            if gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)
            self._last = time.monotonic()


class _TTLCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic(), value)


def _parse_experience(text: str) -> tuple[float | None, float | None]:
    """Naukri returns strings like '3-6 Yrs' or '5+ Yrs'."""
    if not text:
        return None, None
    digits = [int(t) for t in "".join(c if c.isdigit() else " " for c in text).split()]
    if len(digits) >= 2:
        return float(digits[0]), float(digits[1])
    if len(digits) == 1:
        return float(digits[0]), None
    return None, None


def _epoch_ms_to_dt(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class NaukriProvider(JobProvider):
    name = "naukri"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._limiter = RateLimiter(settings.naukri_rate_limit_rps)
        self._cache = _TTLCache(settings.naukri_cache_ttl_seconds)
        headers = {**BASE_HEADERS, "Cookie": settings.naukri_cookie}
        if settings.naukri_auth_token:
            token = settings.naukri_auth_token
            # Accept the token with or without the scheme, since devtools shows it both
            # ways depending on where you copy from.
            headers["Authorization"] = (
                token if token.lower().startswith("bearer ") else f"Bearer {token}"
            )
        self._client = client or httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=True,
            headers=headers,
            verify=build_ssl_context(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.settings.naukri_enabled and self.settings.naukri_cookie)

    def inspect_session(self) -> dict[str, Any]:
        """Static description of the configured credentials. Makes no network request.

        Reports what was actually loaded from the environment so a bad paste is visible
        immediately rather than surfacing as an opaque 403 on the first search.
        """
        cookie = self.settings.naukri_cookie
        names = [
            part.split("=", 1)[0].strip()
            for part in cookie.split(";")
            if "=" in part
        ]
        # Cookies that carry the login on Naukri. Presence is a strong hint the paste is
        # the authenticated header and not, say, a pre-login one.
        # Reported lowercased: matching is case-insensitive, so echoing the source
        # casing would make the same cookie look like two different findings.
        auth_markers = sorted({n.lower() for n in names if n.lower() in _AUTH_COOKIE_HINTS})
        problems: list[str] = []
        if not cookie:
            problems.append("NAUKRI_COOKIE is empty.")
        elif not names:
            problems.append(
                "NAUKRI_COOKIE has no name=value pairs -- you may have copied a single "
                "cookie's value instead of the whole Cookie header."
            )
        elif not auth_markers:
            problems.append(
                "No login cookie found among "
                f"{', '.join(sorted(_AUTH_COOKIE_HINTS))}. You were probably logged out "
                "in the tab you copied from, or copied from a pre-login page."
            )
        if not self.settings.naukri_enabled:
            problems.append("NAUKRI_ENABLED is false, so the provider stays disabled.")

        return {
            "enabled": self.enabled,
            "cookie_present": bool(cookie),
            "cookie_length": len(cookie),
            "cookie_count": len(names),
            "login_cookies_found": auth_markers,
            "authorization_header_set": bool(self.settings.naukri_auth_token),
            "rate_limit_rps": self.settings.naukri_rate_limit_rps,
            "problems": problems,
        }

    async def probe(self) -> dict[str, Any]:
        """One real, minimal request to see whether the session actually authenticates."""
        report = self.inspect_session()
        if not self.enabled:
            report["live_check"] = "skipped -- provider disabled or no cookie"
            return report
        try:
            await self.search(SearchQuery(keywords="python", limit=1))
        except ProviderBlocked as exc:
            report["live_check"] = "blocked"
            report["detail"] = str(exc)
        except ProviderError as exc:
            report["live_check"] = "error"
            report["detail"] = str(exc)
        else:
            report["live_check"] = "ok"
            report["detail"] = "Naukri accepted the session and returned results."
        return report

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        cache_key = f"{url}?{json.dumps(params, sort_keys=True)}"
        if (cached := self._cache.get(cache_key)) is not None:
            return cached

        await self._limiter.wait()
        try:
            response = await self._client.get(url, params=params)
        except httpx.RequestError as exc:
            raise ProviderError(f"Network error talking to Naukri: {exc}") from exc

        if response.status_code in (401, 403):
            raise ProviderBlocked(
                "Naukri rejected the request (HTTP "
                f"{response.status_code}). Your NAUKRI_COOKIE is probably expired -- log "
                "in at naukri.com in your browser and copy a fresh cookie. If it keeps "
                "failing, they are blocking automated access; paste the JD into "
                "parse_job_description instead."
            )
        if response.status_code == 429:
            raise ProviderBlocked(
                "Naukri rate-limited us (HTTP 429). Lower NAUKRI_RATE_LIMIT_RPS and wait "
                "a few minutes before retrying."
            )
        if response.status_code >= 400:
            raise ProviderBlocked(
                f"Naukri returned HTTP {response.status_code}. Their internal API most "
                "likely changed shape -- this adapter is unofficial and unsupported. Use "
                "parse_job_description in the meantime."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            # An HTML body here almost always means a login wall or bot interstitial.
            raise ProviderBlocked(
                "Naukri returned HTML instead of JSON, which means a login wall or bot "
                "check. Refresh your NAUKRI_COOKIE, or paste the JD instead."
            ) from exc

        self._cache.set(cache_key, payload)
        return payload

    async def search(self, query: SearchQuery) -> list[JobPosting]:
        if not self.enabled:
            raise ProviderBlocked(
                "Naukri provider is disabled. Set NAUKRI_ENABLED=true and NAUKRI_COOKIE "
                "in your .env to use it, or paste the JD into parse_job_description."
            )

        params: dict[str, Any] = {
            "noOfResults": min(query.limit, 100),
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": query.keywords,
            "pageNo": 1,
            "seoKey": query.keywords.replace(" ", "-").lower() + "-jobs",
            "src": "jobsearchDesk",
            "latLong": "",
        }
        if query.location:
            params["location"] = query.location
        if query.experience_years is not None:
            params["experience"] = int(query.experience_years)

        payload = await self._get(SEARCH_URL, params)
        details = payload.get("jobDetails")
        if details is None:
            raise ProviderBlocked(
                "Naukri's response had no 'jobDetails' field, so their API shape has "
                "changed. Paste the JD into parse_job_description instead."
            )
        return [self._to_posting(item) for item in details if isinstance(item, dict)]

    async def fetch_detail(self, provider_job_id: str) -> JobPosting | None:
        if not self.enabled:
            return None
        payload = await self._get(DETAIL_URL.format(job_id=provider_job_id))
        detail = payload.get("jobDetails") or payload.get("jd") or {}
        if not isinstance(detail, dict) or not detail:
            return None
        posting = self._to_posting(detail)
        # The detail endpoint carries the full HTML description; the search hit does not.
        html = detail.get("description") or detail.get("jobDescription") or ""
        if html:
            from naukri_mcp.providers.manual import html_to_text

            posting.description = html_to_text(html)
        return posting

    def _to_posting(self, item: dict[str, Any]) -> JobPosting:
        placeholders = item.get("placeholders") or []
        by_type = {
            p.get("type"): p.get("label")
            for p in placeholders
            if isinstance(p, dict)
        }
        exp_min, exp_max = _parse_experience(by_type.get("experience", "") or "")

        tags = item.get("tagsAndSkills") or ""
        skills = [s.strip() for s in tags.split(",") if s.strip()] if isinstance(tags, str) else []

        job_id = str(item.get("jobId") or item.get("jobid") or "")
        url = item.get("jdURL") or ""
        if url and url.startswith("/"):
            url = f"https://www.naukri.com{url}"

        raw_description = item.get("jobDescription") or item.get("description") or ""
        if raw_description and "<" in raw_description:
            from naukri_mcp.providers.manual import html_to_text

            raw_description = html_to_text(raw_description)

        return JobPosting(
            provider=self.name,
            provider_job_id=job_id,
            title=str(item.get("title") or "")[:512],
            company=str(item.get("companyName") or "")[:512],
            location=str(by_type.get("location") or "")[:512],
            description=raw_description,
            url=url,
            apply_url=url,
            experience_min=exp_min,
            experience_max=exp_max,
            salary_text=str(by_type.get("salary") or "")[:256],
            posted_at=_epoch_ms_to_dt(item.get("createdDate")),
            skills=skills,
            raw={"jobId": job_id},
        )
