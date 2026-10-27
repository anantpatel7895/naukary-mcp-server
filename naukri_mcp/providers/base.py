"""Job provider interface.

One adapter per source. `manual` (paste a JD) always works and has no dependencies;
`naukri` is opt-in and may be blocked at any time. Adding Adzuna later means adding
one file that implements `JobProvider` -- nothing above this layer changes.
"""

from __future__ import annotations

import abc
from datetime import datetime

from pydantic import BaseModel, Field


class ProviderError(RuntimeError):
    """Base for provider failures."""


class ProviderBlocked(ProviderError):
    """The source refused us (403/429/captcha/login wall).

    Raised as a distinct type so tools can tell the user to fall back to
    `parse_job_description` instead of reporting a generic crash.
    """


class ProviderDisabled(ProviderError):
    """The provider exists but is switched off in config."""


class ProviderUnsupported(ProviderError):
    """This provider does not implement the requested operation."""


class SearchQuery(BaseModel):
    keywords: str = ""
    location: str = ""
    experience_years: float | None = None
    limit: int = Field(default=20, ge=1, le=100)
    posted_within_days: int | None = None


class JobPosting(BaseModel):
    """Normalized job, provider-agnostic."""

    provider: str
    provider_job_id: str
    title: str
    company: str = ""
    location: str = ""
    description: str = ""
    url: str = ""
    apply_url: str = ""
    contact_email: str = ""
    experience_min: float | None = None
    experience_max: float | None = None
    salary_text: str = ""
    posted_at: datetime | None = None
    skills: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class JobProvider(abc.ABC):
    name: str

    @property
    def enabled(self) -> bool:
        return True

    @abc.abstractmethod
    async def search(self, query: SearchQuery) -> list[JobPosting]:
        """Return matching postings. Raise ProviderBlocked if the source refuses."""

    async def fetch_detail(self, provider_job_id: str) -> JobPosting | None:
        """Full JD for one posting. Return None if this provider cannot fetch details."""
        return None

    async def aclose(self) -> None:
        """Release any network resources."""
        return None
