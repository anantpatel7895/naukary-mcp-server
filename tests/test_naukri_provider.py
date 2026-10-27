"""Naukri adapter behaviour, against mocked HTTP.

The live endpoint is unofficial and blocks automated access, so these tests pin the two
things that matter: that we parse the payload shape correctly, and that every kind of
refusal surfaces as ProviderBlocked with actionable advice rather than a stack trace.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from naukri_mcp.config import Settings
from naukri_mcp.providers.base import ProviderBlocked, SearchQuery
from naukri_mcp.providers.naukri import SEARCH_URL, NaukriProvider

SEARCH_PAYLOAD = {
    "jobDetails": [
        {
            "jobId": "010101000000",
            "title": "Senior Python Engineer",
            "companyName": "Example Technologies",
            "jdURL": "/job-listings-senior-python-engineer-example-010101000000",
            "tagsAndSkills": "Python,FastAPI,PostgreSQL,Docker",
            "jobDescription": "<p>Build <b>Python</b> services.</p><ul><li>FastAPI</li></ul>",
            "createdDate": "1767225600000",
            "placeholders": [
                {"type": "experience", "label": "3-6 Yrs"},
                {"type": "salary", "label": "20-30 Lacs PA"},
                {"type": "location", "label": "Bangalore"},
            ],
        }
    ]
}


def make_settings(**overrides) -> Settings:
    base = dict(
        naukri_enabled=True,
        naukri_cookie="test-cookie",
        naukri_rate_limit_rps=1000.0,  # no real sleeping in tests
        naukri_cache_ttl_seconds=900,
    )
    base.update(overrides)
    return Settings(**base)


@respx.mock
async def test_search_parses_payload():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH_PAYLOAD))
    provider = NaukriProvider(make_settings())
    try:
        results = await provider.search(SearchQuery(keywords="python", location="Bangalore"))
    finally:
        await provider.aclose()

    assert len(results) == 1
    job = results[0]
    assert job.provider == "naukri"
    assert job.provider_job_id == "010101000000"
    assert job.title == "Senior Python Engineer"
    assert job.company == "Example Technologies"
    assert job.location == "Bangalore"
    assert (job.experience_min, job.experience_max) == (3.0, 6.0)
    assert job.salary_text == "20-30 Lacs PA"
    assert job.skills == ["Python", "FastAPI", "PostgreSQL", "Docker"]
    # Relative jdURL is absolutized, and the HTML description is flattened to text.
    assert job.url.startswith("https://www.naukri.com/")
    assert "<p>" not in job.description
    assert "Python" in job.description
    assert job.posted_at is not None


@respx.mock
async def test_expired_cookie_raises_provider_blocked_with_advice():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(403))
    provider = NaukriProvider(make_settings())
    try:
        with pytest.raises(ProviderBlocked) as exc:
            await provider.search(SearchQuery(keywords="python"))
    finally:
        await provider.aclose()
    message = str(exc.value)
    assert "NAUKRI_COOKIE" in message
    assert "parse_job_description" in message


@respx.mock
async def test_rate_limited_raises_provider_blocked():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))
    provider = NaukriProvider(make_settings())
    try:
        with pytest.raises(ProviderBlocked, match="rate-limited"):
            await provider.search(SearchQuery(keywords="python"))
    finally:
        await provider.aclose()


@respx.mock
async def test_html_login_wall_raises_provider_blocked():
    """A bot interstitial returns HTML with a 200 -- the sneakiest failure mode."""
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, text="<html><body>Please log in</body></html>")
    )
    provider = NaukriProvider(make_settings())
    try:
        with pytest.raises(ProviderBlocked, match="HTML instead of JSON"):
            await provider.search(SearchQuery(keywords="python"))
    finally:
        await provider.aclose()


@respx.mock
async def test_changed_api_shape_raises_provider_blocked():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"somethingElse": []}))
    provider = NaukriProvider(make_settings())
    try:
        with pytest.raises(ProviderBlocked, match="shape has changed"):
            await provider.search(SearchQuery(keywords="python"))
    finally:
        await provider.aclose()


async def test_disabled_provider_refuses_clearly():
    provider = NaukriProvider(make_settings(naukri_enabled=False))
    try:
        with pytest.raises(ProviderBlocked, match="NAUKRI_ENABLED"):
            await provider.search(SearchQuery(keywords="python"))
    finally:
        await provider.aclose()


@respx.mock
async def test_repeat_search_is_served_from_cache():
    """Caching is what keeps request volume low against a source that blocks scrapers."""
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH_PAYLOAD))
    provider = NaukriProvider(make_settings())
    try:
        query = SearchQuery(keywords="python", location="Bangalore")
        await provider.search(query)
        await provider.search(query)
    finally:
        await provider.aclose()
    assert route.call_count == 1


async def test_rate_limiter_enforces_minimum_interval():
    import time

    from naukri_mcp.providers.naukri import RateLimiter

    limiter = RateLimiter(rps=20.0)  # 50ms apart
    start = time.monotonic()
    await limiter.wait()
    await limiter.wait()
    assert time.monotonic() - start >= 0.045


# --- session setup / diagnostics -------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("nauk_at=abc; ps=1", "nauk_at=abc; ps=1"),
        ('"nauk_at=abc"', "nauk_at=abc"),  # quoted paste
        ("Cookie: nauk_at=abc", "nauk_at=abc"),  # devtools row label
        ("cookie:  nauk_at=abc ", "nauk_at=abc"),  # label + padding
        ("nauk_at=abc;\n  ps=1", "nauk_at=abc; ps=1"),  # wrapped multi-line paste
    ],
)
def test_cookie_paste_is_normalized(raw, expected):
    """A pasted cookie arrives in several shapes; all must work rather than 403 later."""
    assert make_settings(naukri_cookie=raw).naukri_cookie == expected


def test_auth_token_label_is_stripped():
    settings = make_settings(naukri_auth_token="Authorization: Bearer xyz")
    assert settings.naukri_auth_token == "Bearer xyz"


@pytest.mark.parametrize("token,expected", [("xyz", "Bearer xyz"), ("Bearer xyz", "Bearer xyz")])
def test_authorization_header_set_with_or_without_scheme(token, expected):
    provider = NaukriProvider(make_settings(naukri_auth_token=token))
    try:
        assert provider._client.headers["Authorization"] == expected
    finally:
        import asyncio

        asyncio.run(provider.aclose())


def test_no_authorization_header_when_token_absent():
    provider = NaukriProvider(make_settings())
    try:
        assert "Authorization" not in provider._client.headers
    finally:
        import asyncio

        asyncio.run(provider.aclose())


async def test_inspect_session_reports_healthy_paste():
    cookie = "_t_ds=1; nauk_at=eyJhbGciOi; NKWAP=xyz; ninjaid=9"
    provider = NaukriProvider(make_settings(naukri_cookie=cookie))
    try:
        report = provider.inspect_session()
    finally:
        await provider.aclose()
    assert report["enabled"] is True
    assert report["cookie_count"] == 4
    assert set(report["login_cookies_found"]) == {"nauk_at", "ninjaid", "nkwap"}
    assert report["problems"] == []


async def test_inspect_session_flags_single_cookie_value_paste():
    """The most common mistake: copying one cookie's value, not the whole header."""
    provider = NaukriProvider(make_settings(naukri_cookie="eyJhbGciOiJIUzI1NiJ9"))
    try:
        report = provider.inspect_session()
    finally:
        await provider.aclose()
    assert any("no name=value pairs" in p for p in report["problems"])


async def test_inspect_session_flags_logged_out_paste():
    provider = NaukriProvider(make_settings(naukri_cookie="bm_sv=1; _ga=2"))
    try:
        report = provider.inspect_session()
    finally:
        await provider.aclose()
    assert any("No login cookie found" in p for p in report["problems"])


async def test_inspect_session_flags_disabled_flag():
    provider = NaukriProvider(make_settings(naukri_enabled=False))
    try:
        report = provider.inspect_session()
    finally:
        await provider.aclose()
    assert any("NAUKRI_ENABLED" in p for p in report["problems"])


@respx.mock
async def test_probe_reports_ok_on_success():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH_PAYLOAD))
    provider = NaukriProvider(make_settings(naukri_cookie="nauk_at=abc"))
    try:
        report = await provider.probe()
    finally:
        await provider.aclose()
    assert report["live_check"] == "ok"


@respx.mock
async def test_probe_reports_blocked_with_detail():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(403))
    provider = NaukriProvider(make_settings(naukri_cookie="nauk_at=abc"))
    try:
        report = await provider.probe()
    finally:
        await provider.aclose()
    assert report["live_check"] == "blocked"
    assert "NAUKRI_COOKIE" in report["detail"]


def test_registry_excludes_naukri_without_cookie():
    from naukri_mcp.providers.registry import enabled_providers

    assert enabled_providers(make_settings(naukri_cookie="")) == []
    assert [p.name for p in enabled_providers(make_settings())] == ["naukri"]
