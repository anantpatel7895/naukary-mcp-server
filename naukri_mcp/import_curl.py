"""Import a Naukri session from Chrome DevTools' "Copy as cURL".

Picking the right request out of a busy Network tab by eye is error-prone, and hand-
copying a 3 KB cookie is worse. Instead: right-click any naukri.com request in DevTools,
choose Copy > Copy as cURL, then run this.

"Copy as cURL" puts the command on your clipboard, so no file is needed:

    uv run python -m naukri_mcp.import_curl                  # reads the clipboard
    uv run python -m naukri_mcp.import_curl --write          # ...and updates .env

A file or a pipe works too:

    uv run python -m naukri_mcp.import_curl /tmp/naukri.txt --write
    pbpaste | uv run python -m naukri_mcp.import_curl - --write

It tells you whether the request you picked was the search endpoint, and writes the
credentials into .env for you.

Secret hygiene: cookie and token values are never printed. Only their length, the cookie
names, and the non-sensitive headers are shown, so the output is safe to share when
diagnosing. Delete the cURL file afterwards -- it contains your live session.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

#: Headers that carry credentials. Reported by shape only, never by value.
SECRET_HEADERS = {"cookie", "authorization", "x-csrf-token"}

#: Headers worth knowing about when reconstructing the request shape.
INTERESTING_HEADERS = {"appid", "systemid", "content-type", "accept", "referer", "user-agent"}


class CurlParseError(ValueError):
    pass


def parse_curl(text: str) -> tuple[str, dict[str, str], str]:
    """Return (url, headers, body) from a copied cURL command."""
    text = text.strip()
    if not text:
        raise CurlParseError("The file is empty.")
    if "curl" not in text[:200].lower():
        raise CurlParseError(
            "This does not look like a cURL command. In DevTools right-click the "
            "request > Copy > Copy as cURL (bash on macOS/Linux)."
        )

    # Join the shell line continuations Chrome emits, then split like a shell would.
    normalized = re.sub(r"\\\r?\n", " ", text)
    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        raise CurlParseError(f"Could not parse the command: {exc}") from exc

    url = ""
    headers: dict[str, str] = {}
    body = ""

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "curl":
            i += 1
            continue
        if token in ("-H", "--header") and i + 1 < len(tokens):
            name, _, value = tokens[i + 1].partition(":")
            headers[name.strip().lower()] = value.strip()
            i += 2
            continue
        if token in ("-b", "--cookie") and i + 1 < len(tokens):
            headers["cookie"] = tokens[i + 1].strip()
            i += 2
            continue
        if token in ("--data", "--data-raw", "--data-binary", "-d") and i + 1 < len(tokens):
            body = tokens[i + 1]
            i += 2
            continue
        if token.startswith("-"):
            # Flags we do not care about (--compressed, -X GET, etc.).
            i += 2 if token in ("-X", "--request") else 1
            continue
        if not url and token.startswith("http"):
            url = token
        i += 1

    if not url:
        raise CurlParseError("No URL found in the command.")
    return url, headers, body


def describe(url: str, headers: dict[str, str]) -> dict[str, object]:
    """Classify the captured request and summarize it without leaking secrets."""
    parsed = urlparse(url)
    params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}

    cookie = headers.get("cookie", "")
    cookie_names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]

    path = parsed.path
    if "/jobapi/" in path and "search" in path:
        kind = "search"
    elif "/jobapi/" in path and "/job/" in path:
        kind = "job detail"
    elif "/jobapi/" in path:
        kind = "other jobapi call"
    else:
        kind = "not a jobapi call"

    return {
        "host": parsed.netloc,
        "path": path,
        "kind": kind,
        "query_params": params,
        "cookie_present": bool(cookie),
        "cookie_length": len(cookie),
        "cookie_names": cookie_names,
        "authorization_present": "authorization" in headers,
        "notable_headers": {
            k: v for k, v in sorted(headers.items()) if k in INTERESTING_HEADERS
        },
        "other_header_names": sorted(
            k for k in headers if k not in INTERESTING_HEADERS and k not in SECRET_HEADERS
        ),
    }


def update_env(env_path: Path, values: dict[str, str]) -> list[str]:
    """Set keys in .env, replacing existing lines. Returns the keys written."""
    lines = (
        env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    )
    written: list[str] = []
    for key, value in values.items():
        if not value:
            continue
        replacement = f"{key}={value}"
        for idx, line in enumerate(lines):
            if line.strip().startswith(f"{key}=") or line.strip().startswith(f"#{key}="):
                lines[idx] = replacement
                break
        else:
            lines.append(replacement)
        written.append(key)

    if written:
        env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return written


CLIPBOARD_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("pbpaste",),  # macOS
    ("wl-paste",),  # Wayland
    ("xclip", "-selection", "clipboard", "-o"),  # X11
    ("xsel", "--clipboard", "--output"),
)


def read_clipboard() -> str:
    """Return clipboard text, or '' if no clipboard tool is available."""
    import shutil
    import subprocess

    for command in CLIPBOARD_COMMANDS:
        if shutil.which(command[0]) is None:
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    return ""


def _load_input(args: list[str]) -> tuple[str, str]:
    """Return (text, description_of_where_it_came_from)."""
    if args and args[0] != "-":
        path = Path(args[0]).expanduser()
        if not path.exists():
            raise SystemExit(
                f"No such file: {path}\n\n"
                '"Copy as cURL" puts the command on your clipboard, not on disk. Either:\n'
                "  - run this with no filename at all, and it reads the clipboard:\n"
                "      uv run python -m naukri_mcp.import_curl --write\n"
                f"  - or save the clipboard to that file first:\n"
                f"      pbpaste > {path}"
            )
        return path.read_text(encoding="utf-8"), str(path)

    if args and args[0] == "-":
        return sys.stdin.read(), "stdin"

    if not sys.stdin.isatty():
        # Piped input, e.g. `pbpaste | python -m naukri_mcp.import_curl`.
        piped = sys.stdin.read()
        if piped.strip():
            return piped, "stdin"

    text = read_clipboard()
    if not text.strip():
        raise SystemExit(
            "Nothing on the clipboard, and no file given.\n\n"
            "In Chrome DevTools, right-click any naukri.com request >\n"
            "Copy > Copy as cURL, then run this again."
        )
    return text, "clipboard"


def main() -> None:
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("-") or a == "-"]
    write = "--write" in argv

    if "-h" in argv or "--help" in argv:
        print(__doc__)
        raise SystemExit(0)

    text, origin = _load_input(args)
    print(f"Read cURL command from {origin}.\n")

    try:
        url, headers, _body = parse_curl(text)
    except CurlParseError as exc:
        print(f"Could not read that cURL command:\n  {exc}")
        raise SystemExit(1) from None

    info = describe(url, headers)

    print("Captured request")
    print("-" * 60)
    print(f"  host              {info['host']}")
    print(f"  path              {info['path']}")
    print(f"  looks like        {info['kind']}")
    print(f"  cookie            {'yes' if info['cookie_present'] else 'NO'}"
          f" ({info['cookie_length']} chars, {len(info['cookie_names'])} cookies)")
    print(f"  authorization     {'yes' if info['authorization_present'] else 'no'}")

    if info["query_params"]:
        print("\n  Query parameters (this is the endpoint shape):")
        for key, value in info["query_params"].items():
            print(f"    {key} = {value}")

    if info["notable_headers"]:
        print("\n  Notable headers:")
        for key, value in info["notable_headers"].items():
            shown = value if len(value) < 70 else value[:67] + "..."
            print(f"    {key}: {shown}")

    # Names only, no values. A site-specific header here (a signed nonce, a request
    # signature) is often the reason a replayed request is rejected, so seeing the full
    # list matters when the adapter gets refused.
    if info["other_header_names"]:
        print("\n  Other headers present (names only):")
        print(f"    {', '.join(info['other_header_names'])}")

    if info["host"] and "naukri.com" not in str(info["host"]):
        print(f"\n  WARNING: host is {info['host']}, not naukri.com. Wrong request?")

    if not info["cookie_present"]:
        print("\n  No cookie on this request. Pick one sent while logged in.")
        raise SystemExit(1)

    if write:
        values = {"NAUKRI_ENABLED": "true", "NAUKRI_COOKIE": headers["cookie"]}
        if "authorization" in headers:
            values["NAUKRI_AUTH_TOKEN"] = headers["authorization"]
        env_path = Path(".env")
        written = update_env(env_path, values)
        print(f"\n  Wrote {', '.join(written)} to {env_path.resolve()}")
        print("  Restart the MCP server, then verify:")
        print("    uv run python -m naukri_mcp.check_session --live")
        # The source still holds a live session, so say so -- but only for sources that
        # persist. A clipboard read has nothing to clean up.
        if origin not in ("clipboard", "stdin"):
            print(f"\n  Now delete {origin} -- it contains your live session.")
    else:
        print("\n  Inspect only. Re-run with --write to update .env.")


if __name__ == "__main__":
    main()
