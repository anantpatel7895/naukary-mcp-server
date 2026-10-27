# Naukri MCP Server

An MCP server for job hunting: search jobs, tailor your resume for ATS keyword matching,
apply, and track every application — driven from Claude Desktop, Claude Code, Cursor, or
any MCP-compatible client.

The core idea: **your resume is the source of truth.** The server reorders, rewords, and
surfaces what is genuinely in it to match each job description. It will not add a skill
you do not have — that is enforced by a validator, not by a prompt.

---

## The anti-fabrication guarantee

Most "AI resume tailoring" quietly inflates. This does not, structurally.

`analyze_job_match` sorts every keyword in the job description into one of four buckets:

| Bucket | Meaning | What may be done with it |
| --- | --- | --- |
| `have_surfaced` | In your resume and already prominent | Leave alone |
| `have_buried` | In your resume but not visible early | Promote into a lead bullet |
| `adjacent` | You have a *sibling* skill only (FAISS vs Pinecone) | Frame honestly; never claim it |
| `missing` | Absent from your resume entirely | **Never added.** Learning target only |

Then `render_resume` **validates before writing anything**:

- every tailored bullet must carry an `evidence_ref` pointing at a real line in your resume;
- no bullet may assert a `missing` keyword;
- no bullet may claim an `adjacent` keyword as your own;
- skill reordering may not introduce a skill you do not have.

Any violation rejects the whole proposal and writes no files, naming the offending line.
The worst case is a resume that scores lower. It is never one that lies.

There is a fifth relationship the analyzer models separately, because collapsing it into
`adjacent` produced wrong answers: **entailment**. FastAPI genuinely proves REST API
experience, so "built REST APIs" is true and permitted, reported as
`implied_by: ["fastapi"]`. Chroma does not prove Pinecone, so that stays blocked.

It also flags `weak_claim`: a skill you list but never demonstrate in any achievement
bullet. That passes keyword screens but is where an interviewer will press you.

---

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/anantpatel7895/naukary-mcp-server
cd naukary-mcp-server
uv sync
cp .env.example .env      # every value is optional; defaults are safe
```

Behind a corporate TLS proxy, add `--system-certs` to uv commands
(`uv sync --system-certs`).

### Register with your MCP client

Claude Code:

```bash
claude mcp add naukri -- uv --directory /absolute/path/to/naukary-mcp-server run naukri-mcp
```

Claude Desktop — add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "naukri": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/naukary-mcp-server", "run", "naukri-mcp"]
    }
  }
}
```

Inspect the tools directly with `uv run mcp dev naukri_mcp/server.py`.

---

## Usage

Import your resume once:

> import my resume from ~/Downloads/Resume.pdf

Check the reported counts. If a section is missing, the parser hit an unusual heading —
paste that section and it can be taught.

Then, per job:

> Here's a JD I found: *(paste it)* — analyze how well I match, then tailor my resume

The flow behind that request:

1. `parse_job_description(text=...)` → a `job_id`
2. `analyze_job_match(job_id)` → gap report + explainable ATS score
3. Claude writes tailored bullets, each citing an `evidence_ref`, and shows you the diff
4. `render_resume(job_id, bullets=[...])` → validates, then writes DOCX + PDF
5. `apply_to_job(job_id)` → email or assisted apply, after your confirmation

There is a `tailor_resume` prompt that packages steps 1–5 if you prefer.

### Tools

| Tool | Purpose |
| --- | --- |
| `import_resume` | Parse PDF/DOCX/TXT into the master profile |
| `parse_job_description` | Add a job from pasted text or a URL |
| `search_jobs` | Search enabled providers (see the Naukri note below) |
| `check_naukri_session` | Diagnose the Naukri cookie/header; `live=true` tests it for real |
| `get_job` | Full stored JD |
| `analyze_job_match` | Keyword gap report + ATS score. Read-only |
| `render_resume` | Validate tailoring, then write ATS-safe DOCX + PDF |
| `apply_to_job` | Email apply or assisted apply; refuses duplicates |
| `list_applications` | Pipeline view + a 7-day follow-up queue |
| `update_application_status` | Move an application along the pipeline |
| `variant_performance` | Response rate per resume variant |

Resource `resume://master` exposes the parsed profile and every citable `evidence_ref`.

### Why DOCX is the primary output

Most ATS parse DOCX more reliably than PDF. The renderer emits a single column with no
tables, text boxes, images, headers/footers, or multi-column layout — those are precisely
what makes parsers drop content. Contact details sit in the body, and job titles share a
line with their dates, because parsers anchor a role on the line carrying the date range.
A test round-trips the generated DOCX back through the resume parser to keep it honest.

---

## Safety defaults

- `DRY_RUN=true` — emails are drafted and logged, never transmitted, until you set it false.
- `apply_to_job(mode="email")` returns a **draft** first. It sends only when called again
  with `confirm_send=true`, after you have seen it.
- Applying twice to the same job is refused. A dry run does not count as applied, so you
  can still apply for real afterwards.
- Every send attempt, including dry runs, is written to `email_log`.
- Nothing leaves your machine except the SMTP send you explicitly confirm.

---

## About the Naukri adapter

**Naukri publishes no public job-search API.** Its Terms prohibit automated access, and
its `robots.txt` (edited 2026-05-13) sets `Disallow: /` for AI agents — `Claude-User`,
`claudebot`, `gptbot`, `perplexitybot` and others — permitting only `/blog/`,
`/career-advice/`, `/code360/`, `/recruit/` and `/resume-maker`. Job search and JD pages
are disallowed for bots.

The adapter is therefore **off by default**, and when enabled it:

- uses **your own logged-in session** (`NAUKRI_COOKIE`, plus `NAUKRI_AUTH_TOKEN` if your
  account sends one) rather than crawling anonymously — no browser is automated;
- runs at roughly one request every three seconds, single-user volume;
- caches responses for 15 minutes so repeated analysis never re-requests;
- raises a clear `ProviderBlocked` with next steps the moment it is refused.

**Setup and cookie refresh: [`docs/naukri-session.md`](docs/naukri-session.md).** The
session expires periodically, so re-pasting is routine.

The quickest way in: right-click any naukri.com request in DevTools → **Copy as cURL**,
then

```bash
uv run python -m naukri_mcp.import_curl --write   # reads clipboard, cookie + token -> .env
uv run python -m naukri_mcp.check_session --live  # confirm Naukri accepts it
```

No hunting for the "right" request — every naukri.com request carries the same cookie.
Neither command ever prints the cookie value. You can also ask Claude to *"check my
naukri session"* (`check_naukri_session`).

**Expect it to break without warning.** Enabling it is your call, and it may put your
Naukri account at risk. Everything else here is built to work without it: pasting a JD
into `parse_job_description` needs no keys, no network, and no permission, and it is the
recommended path.

Adding a stable, sanctioned source later is one file: implement `JobProvider` in
`naukri_mcp/providers/` and add a branch to `registry.py`.
[Adzuna](https://developer.adzuna.com/) is the obvious candidate — a free tier of 1,000
calls/month with genuine India coverage and an official API key.

---

## Database

SQLite by default at `~/.naukri-mcp/jobs.db`; set `DATABASE_URL` for Postgres. Tables are
created on startup. Tables: `master_profile`, `resume_variant`, `job`, `application`,
`application_event`, `email_log`, `company_blocklist`, `saved_search`.

Re-importing a resume retires the previous profile rather than deleting it, so old
variants stay explainable.

---

## Development

```bash
uv run pytest -q            # 54 tests
uv run mcp dev naukri_mcp/server.py
```

`tests/test_validator.py` is the important one. If anything there fails, the server can
emit a resume that claims something untrue — fix it before using the tool.

Extending the keyword vocabulary is the highest-leverage tweak: `SKILL_VOCAB`,
`ADJACENT_SKILLS` (siblings), and `IMPLIES` (entailment) in `naukri_mcp/resume/keywords.py`.
`analyze_job_match` reports `unknown_terms_for_review` to show you what it did not
recognize.

## Not built yet

Ranked by payoff: variant A/B analytics over a real sample, automatic follow-up
reminders, cover-letter generation from the same validated evidence, cross-posting dedupe,
interview prep packs, saved searches with a scheduled digest, and an Adzuna adapter.
