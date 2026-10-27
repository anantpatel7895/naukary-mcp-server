"""Test isolation.

Settings loads `.env` by default, which means a developer's real credentials leak into
the test run: tests pass or fail depending on whether you happen to have a Naukri cookie
configured, and real secrets end up in assertion output. This fixture severs that link
for every test, so the suite only ever sees values a test set deliberately.
"""

from __future__ import annotations

import os

import pytest

#: Prefixes covering every credential-bearing setting.
_LEAKY_PREFIXES = ("NAUKRI_", "SMTP_", "FROM_", "ANTHROPIC_", "GROQ_", "DATABASE_", "DATA_DIR", "DRY_RUN")


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch):
    from naukri_mcp.config import Settings, get_settings

    # Stop pydantic-settings reading the developer's .env.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    # Drop any matching values already exported into the environment.
    for name in list(os.environ):
        if name.startswith(_LEAKY_PREFIXES):
            monkeypatch.delenv(name, raising=False)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
