"""Verify the configured Naukri session from a terminal.

    uv run python -m naukri_mcp.check_session          # inspect .env only, no network
    uv run python -m naukri_mcp.check_session --live   # send one real request

Useful because it does not require an MCP client, so you can paste a cookie and confirm
it before wiring anything up. See docs/naukri-session.md.
"""

from __future__ import annotations

import asyncio
import sys

from naukri_mcp.config import get_settings
from naukri_mcp.providers.naukri import NaukriProvider


async def _run(live: bool) -> int:
    settings = get_settings()
    provider = NaukriProvider(settings)
    try:
        report = await provider.probe() if live else provider.inspect_session()
    finally:
        await provider.aclose()

    print("Naukri session check")
    print("-" * 52)
    for key in (
        "enabled",
        "cookie_present",
        "cookie_length",
        "cookie_count",
        "login_cookies_found",
        "authorization_header_set",
        "rate_limit_rps",
    ):
        print(f"  {key:26} {report[key]}")

    if "live_check" in report:
        print(f"  {'live_check':26} {report['live_check']}")
        if report.get("detail"):
            print(f"\n  {report['detail']}")

    problems = report.get("problems") or []
    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nFix these in .env, then re-run. See docs/naukri-session.md")
        return 1

    if not live:
        print("\nWell-formed. Re-run with --live to confirm Naukri accepts it.")
    elif report.get("live_check") != "ok":
        return 1
    return 0


def main() -> None:
    live = "--live" in sys.argv[1:]
    raise SystemExit(asyncio.run(_run(live)))


if __name__ == "__main__":
    main()
