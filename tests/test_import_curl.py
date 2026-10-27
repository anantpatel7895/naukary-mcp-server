"""Importing a session from DevTools' "Copy as cURL"."""

from __future__ import annotations

from pathlib import Path

import pytest

from naukri_mcp.import_curl import CurlParseError, describe, parse_curl, update_env

# Chrome on macOS emits exactly this shape: backslash-continued lines, single quotes.
CHROME_CURL = r"""curl 'https://www.naukri.com/jobapi/v3/search?noOfResults=20&urlType=search_by_keyword&searchType=adv&keyword=python%20developer&pageNo=1&experience=3' \
  -H 'accept: application/json' \
  -H 'appid: 109' \
  -H 'systemid: Naukri' \
  -H 'cookie: _t_ds=abc; nauk_at=eyJhbGciOi; NKWAP=xyz; ninjaid=9' \
  -H 'authorization: Bearer eyJtok' \
  -H 'referer: https://www.naukri.com/python-developer-jobs' \
  -H 'sec-ch-ua-platform: "macOS"' \
  --compressed"""


def test_parses_chrome_curl():
    url, headers, _ = parse_curl(CHROME_CURL)
    assert url.startswith("https://www.naukri.com/jobapi/v3/search")
    assert headers["appid"] == "109"
    assert headers["systemid"] == "Naukri"
    assert headers["cookie"].startswith("_t_ds=abc")
    assert headers["authorization"] == "Bearer eyJtok"


def test_describe_identifies_search_and_extracts_shape():
    url, headers, _ = parse_curl(CHROME_CURL)
    info = describe(url, headers)
    assert info["kind"] == "search"
    assert info["path"] == "/jobapi/v3/search"
    # The query params are the whole point: they reveal the real endpoint contract.
    assert info["query_params"]["keyword"] == "python developer"
    assert info["query_params"]["noOfResults"] == "20"
    assert info["query_params"]["urlType"] == "search_by_keyword"
    assert info["cookie_names"] == ["_t_ds", "nauk_at", "NKWAP", "ninjaid"]
    assert info["authorization_present"] is True


def test_describe_never_exposes_secret_values():
    """The report is meant to be safe to share while debugging."""
    url, headers, _ = parse_curl(CHROME_CURL)
    info = describe(url, headers)
    blob = repr(info)
    assert "eyJhbGciOi" not in blob, "cookie value leaked into the report"
    assert "eyJtok" not in blob, "bearer token leaked into the report"
    assert info["cookie_length"] > 0  # shape is reported instead


def test_identifies_job_detail_request():
    url, headers, _ = parse_curl(
        "curl 'https://www.naukri.com/jobapi/v4/job/010101' -H 'cookie: a=1'"
    )
    assert describe(url, headers)["kind"] == "job detail"


def test_identifies_non_jobapi_request():
    url, headers, _ = parse_curl(
        "curl 'https://www.naukri.com/mnjuser/profile' -H 'cookie: a=1'"
    )
    assert describe(url, headers)["kind"] == "not a jobapi call"


def test_cookie_flag_form_is_supported():
    _url, headers, _ = parse_curl("curl 'https://www.naukri.com/x' -b 'nauk_at=abc'")
    assert headers["cookie"] == "nauk_at=abc"


def test_rejects_non_curl_input():
    with pytest.raises(CurlParseError, match="does not look like a cURL"):
        parse_curl("https://www.naukri.com/jobapi/v3/search")


def test_rejects_empty_input():
    with pytest.raises(CurlParseError, match="empty"):
        parse_curl("   ")


# --- input sources ---------------------------------------------------------------


def test_missing_file_explains_the_clipboard_step(tmp_path: Path):
    """The failure people actually hit: 'Copy as cURL' never wrote a file."""
    from naukri_mcp.import_curl import _load_input

    with pytest.raises(SystemExit) as exc:
        _load_input([str(tmp_path / "nope.txt")])
    message = str(exc.value)
    assert "No such file" in message
    assert "clipboard" in message
    assert "pbpaste" in message


def test_reads_from_file(tmp_path: Path):
    from naukri_mcp.import_curl import _load_input

    path = tmp_path / "c.txt"
    path.write_text(CHROME_CURL, encoding="utf-8")
    text, origin = _load_input([str(path)])
    assert "jobapi" in text
    assert origin == str(path)


def test_reads_from_clipboard_when_no_args(monkeypatch):
    import naukri_mcp.import_curl as mod

    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(mod, "read_clipboard", lambda: CHROME_CURL)
    text, origin = mod._load_input([])
    assert origin == "clipboard"
    assert "jobapi" in text


def test_empty_clipboard_gives_actionable_error(monkeypatch):
    import naukri_mcp.import_curl as mod

    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(mod, "read_clipboard", lambda: "")
    with pytest.raises(SystemExit, match="Copy as cURL"):
        mod._load_input([])


def test_read_clipboard_returns_empty_when_no_tool_installed(monkeypatch):
    """Linux boxes without xclip, and sandboxes, must degrade rather than crash."""
    import naukri_mcp.import_curl as mod

    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert mod.read_clipboard() == ""


def test_read_clipboard_survives_a_hanging_tool(monkeypatch):
    """A clipboard helper that blocks must not hang the CLI forever."""
    import subprocess

    import naukri_mcp.import_curl as mod

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **kwargs):
        assert kwargs.get("timeout"), "clipboard reads must pass a timeout"
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mod.read_clipboard() == ""


def test_update_env_appends_then_replaces(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("DRY_RUN=true\nNAUKRI_COOKIE=old\n", encoding="utf-8")

    update_env(env, {"NAUKRI_COOKIE": "new", "NAUKRI_AUTH_TOKEN": "tok"})
    text = env.read_text()

    assert "NAUKRI_COOKIE=new" in text
    assert "NAUKRI_COOKIE=old" not in text, "the stale cookie must be replaced, not duplicated"
    assert "NAUKRI_AUTH_TOKEN=tok" in text
    assert "DRY_RUN=true" in text, "unrelated settings must survive"
    assert text.count("NAUKRI_COOKIE=") == 1


def test_update_env_creates_file(tmp_path: Path):
    env = tmp_path / ".env"
    written = update_env(env, {"NAUKRI_COOKIE": "abc"})
    assert written == ["NAUKRI_COOKIE"]
    assert env.read_text().strip() == "NAUKRI_COOKIE=abc"


def test_update_env_skips_blank_values(tmp_path: Path):
    env = tmp_path / ".env"
    assert update_env(env, {"NAUKRI_AUTH_TOKEN": ""}) == []
    assert not env.exists()
