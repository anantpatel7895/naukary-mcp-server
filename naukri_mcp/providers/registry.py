"""Which providers are usable given the current config."""

from __future__ import annotations

from naukri_mcp.config import Settings
from naukri_mcp.providers.base import JobProvider


def enabled_providers(settings: Settings) -> list[JobProvider]:
    """Search-capable providers only.

    ManualProvider is excluded: it parses a JD you supply, it does not search. Adding
    Adzuna or JSearch later means appending one branch here.
    """
    providers: list[JobProvider] = []
    if settings.naukri_enabled and settings.naukri_cookie:
        from naukri_mcp.providers.naukri import NaukriProvider

        providers.append(NaukriProvider(settings))
    return providers
