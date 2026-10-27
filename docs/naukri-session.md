# Getting your Naukri session into the MCP server

The Naukri adapter has no API key, because Naukri has no public API. It authenticates by
replaying the session your own browser already has. So the setup is: **log in normally,
copy two request headers, paste them into `.env`, restart the server.**

Read [the caveats](#before-you-start) first — this is opt-in for a reason.

---

## Before you start

- Naukri's Terms prohibit automated access, and its `robots.txt` disallows AI agents from
  job pages. Enabling this is your decision and carries some risk to your account.
- The session **expires**, typically within hours to a couple of weeks. Re-pasting is a
  routine chore, not a one-time setup. That is why this doc exists.
- **You do not need any of this** to use the server. Pasting a job description into
  `parse_job_description` needs no cookie, no network, and no permission — and the whole
  tailoring, applying, and tracking pipeline works from there.

Never paste your cookie into a chat, an issue, or a commit. It is equivalent to your
password for the lifetime of the session. `.gitignore` already excludes `.env`.

---

## Step 1 — Log in to Naukri in your browser

Go to <https://www.naukri.com> and sign in as usual. Complete any OTP or captcha. Leave
the tab open and stay on a logged-in page (your profile or a job search results page).

The browser handles login entirely. The server never sees your password.

---

## Which request do I pick? (the short answer: any of them)

DevTools will show **many** `jobapi` requests — search, filters, recommendations,
tracking. It is normal, and for getting your cookie it does not matter which you pick.

**Every request to `naukri.com` carries the identical `Cookie` header.** Same origin,
same cookies. So any `jobapi` row gives you exactly the same value. There is no "correct"
one to hunt for.

It only matters if you want to capture the *endpoint shape* too (the query parameters
Naukri's search actually uses). For that, use the automated path below, which tells you
what you picked.

### Fastest path: Copy as cURL (recommended)

Skip the manual header hunting entirely.

1. In DevTools, right-click **any** naukri.com request while logged in.
2. Choose **Copy → Copy as cURL** (pick *bash* if macOS/Linux offers a choice).
3. Run — no file needed, it reads your clipboard:

```bash
uv run python -m naukri_mcp.import_curl            # inspect first
uv run python -m naukri_mcp.import_curl --write    # write to .env
```

"Copy as cURL" copies to the **clipboard**, not to disk, so there is no file to create.
A file or a pipe works too, if you prefer:

```bash
pbpaste > /tmp/naukri.txt                                          # save it yourself
uv run python -m naukri_mcp.import_curl /tmp/naukri.txt --write
pbpaste | uv run python -m naukri_mcp.import_curl - --write        # or just pipe it
```

It reports what you captured and confirms whether it was the search endpoint:

```
  host              www.naukri.com
  path              /jobapi/v3/search
  looks like        search
  cookie            yes (2145 chars, 14 cookies)
  authorization     yes

  Query parameters (this is the endpoint shape):
    noOfResults = 20
    urlType = search_by_keyword
    keyword = ai ml engineer
```

`--write` sets `NAUKRI_ENABLED`, `NAUKRI_COOKIE`, and `NAUKRI_AUTH_TOKEN` in `.env`,
replacing any stale values and leaving your other settings untouched.

**Your cookie is never printed** — only its length and the cookie names — so the report
above is safe to paste when asking for help. **Delete the cURL file afterwards**; it
contains your live session.

If `looks like` says `not a jobapi call`, the cookie is still perfectly valid. You just
did not capture the search shape, which only matters for debugging the adapter.

The manual route is below if you prefer it.

---

## Step 2 — Open DevTools on a real API request

You want the headers Naukri's own app sends, not the ones on the HTML page.

**Chrome / Edge / Brave**

1. Press `F12` (or `Cmd+Option+I` on macOS) to open DevTools.
2. Click the **Network** tab.
3. Tick **Fetch/XHR** in the filter row, so you only see API calls.
4. In the filter box, type `jobapi`.
5. Back in the page, run any job search or open a job. Requests appear in the list.
6. Click any request whose name starts with `search` or `job`.

**Firefox**

Same, but the filter row is labelled **XHR** and DevTools is under
*Tools → Browser Tools → Web Developer Tools*.

**Safari**

Enable *Develop* first: *Settings → Advanced → Show features for web developers*. Then
*Develop → Show Web Inspector → Network*.

If nothing shows up under `jobapi`, clear the filter and search for `search` instead —
Naukri does move these paths around.

**You will see many matching requests.** For the cookie, any of them works — they all
carry the same `Cookie` header. If you specifically want the search call, click the
🚫 *Clear* icon first, *then* run a job search in the page: the requests that appear are
only the ones your search caused, and the search response is the largest, containing a
`jobDetails` array.

---

## Step 3 — Copy the `Cookie` header

With the request selected, open the **Headers** panel and scroll to
**Request Headers**. Find `Cookie`.

Copy **the entire value**, not one cookie out of it. It is long — typically 1,000–4,000
characters — and looks like:

```
_t_ds=abc123; nauk_at=eyJhbGciOi...; NKWAP=xyz...; ninjaid=...; PS=...; bm_sv=...
```

> In Chrome, right-click the `Cookie` row and choose **Copy value**. If you only see a
> truncated preview, click **view source** at the top of the Request Headers section to
> get the raw text.

Copying the whole header matters: which specific cookie authenticates the request is
Naukri's business and it changes. Copying all of them means you do not have to care.

### Also grab `Authorization`, if it is there

On the same **Request Headers** panel, look for an `Authorization` header. Some accounts
send a bearer token here in addition to the cookie; others rely on the cookie alone.

If you see it, copy the value (`Bearer eyJ...` — with or without the `Bearer ` prefix is
fine, the server normalizes it). If there is no such header, skip it.

---

## Step 4 — Paste into `.env`

In the project root, edit `.env` (create it from `.env.example` if you have not yet):

```bash
cp .env.example .env
```

Set these four lines:

```dotenv
NAUKRI_ENABLED=true
NAUKRI_COOKIE=_t_ds=abc123; nauk_at=eyJhbGciOi...; NKWAP=xyz...; ninjaid=...
NAUKRI_AUTH_TOKEN=eyJhbGciOi...
NAUKRI_RATE_LIMIT_RPS=0.33
```

Notes that save time:

- **Do not quote the value** and do not add a `Cookie:` prefix. If you do anyway, the
  server strips both — but plain is cleaner.
- Keep it on **one line**. If your editor soft-wraps that is fine; a real newline is not.
- Leave `NAUKRI_AUTH_TOKEN` blank if step 3 found no `Authorization` header.
- Leave `NAUKRI_RATE_LIMIT_RPS` at `0.33` (about one request every three seconds) unless
  you have a reason. Raising it is the fastest way to get blocked.

---

## Step 5 — Verify it worked

Two ways. From a terminal:

```bash
uv run python -m naukri_mcp.check_session           # checks the paste, no network
uv run python -m naukri_mcp.check_session --live    # one real request to Naukri
```

Or from Claude, once the server is connected:

> check my naukri session

which calls `check_naukri_session` (add `live=true` for the real request).

A healthy result looks like:

```
  enabled                    True
  cookie_present             True
  cookie_length              2145
  cookie_count               14
  login_cookies_found        ['nauk_at', 'ninjaid', 'nkwap']
  authorization_header_set   True
  live_check                 ok
```

`login_cookies_found` being empty is the usual sign you copied from a logged-out tab.

---

## Step 6 — Restart the server

Configuration is read once at startup, so a new cookie does nothing until you restart.

- **Claude Desktop** — quit and reopen the app.
- **Claude Code** — `claude mcp restart naukri`, or restart your session.
- **`mcp dev`** — `Ctrl+C` and run it again.

---

## Refreshing an expired session

You will know because searches start failing with a message telling you exactly this.
The fix is steps 1–3 and 5–6 again: log in, copy the fresh `Cookie`, replace the value in
`.env`, restart. Nothing else changes, and no data is lost — jobs, applications, and
resume variants all live in the database, not the session.

If you find yourself doing this often, that is the honest cost of an unofficial adapter,
and a good moment to consider the Adzuna path noted in the README: an official key that
does not expire.

---

## Troubleshooting

| What you see | What it means | Fix |
| --- | --- | --- |
| `NAUKRI_ENABLED is false` | Flag not set | Set `NAUKRI_ENABLED=true`, restart |
| `NAUKRI_COOKIE is empty` | Value missing or the server was not restarted | Check `.env`, restart |
| `no name=value pairs` | You copied one cookie's value, not the header | Redo step 3, copy the whole `Cookie` row — or use Copy as cURL, which cannot get this wrong |
| Too many `jobapi` rows to choose from | Normal | Any of them works for the cookie; see [Which request do I pick?](#which-request-do-i-pick-the-short-answer-any-of-them) |
| `No login cookie found` | Copied from a logged-out or pre-login page | Log in, reload, copy from a `jobapi` request |
| `blocked` + *cookie is probably expired* (403) | Session expired or revoked | Refresh the cookie |
| `rate-limited` (429) | Too many requests | Lower `NAUKRI_RATE_LIMIT_RPS`, wait a few minutes |
| `HTML instead of JSON` | Login wall or bot interstitial | Refresh the cookie; if it persists, they are blocking you |
| `API shape has changed` | Naukri changed their internal endpoint | Nothing you can fix — use `parse_job_description` |

Every one of these keeps working as a normal error with next steps, rather than crashing
the server. And in all of them, `parse_job_description` remains available.

---

## What the server does with the credentials

- Stored only in `.env` on your machine. Never written to the database, never logged, and
  `.gitignore` excludes `.env`.
- Sent only to `www.naukri.com`, on the two endpoints in
  `naukri_mcp/providers/naukri.py` (`jobapi/v3/search` and `jobapi/v4/job/{id}`).
- Rate-limited to one request per ~3 seconds and cached for 15 minutes, so repeated
  analysis of the same job re-requests nothing.
- Never used for applying. `apply_to_job` either emails the recruiter address in the JD
  or hands you the apply URL to submit yourself — no browser automation exists in this
  project, and nothing is submitted to Naukri on your behalf.
