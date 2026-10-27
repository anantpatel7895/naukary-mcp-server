"""End-to-end flow over the real MCP protocol using the SDK's in-memory client.

Exercises the whole pipeline the way a connected model would drive it, against a
temporary database so it never touches your real job-hunt data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp import Client

JD = """
Senior Python Backend Engineer

Must have:
- Python and FastAPI for REST API development
- PostgreSQL
- Kubernetes and Docker for deployment

Nice to have:
- Terraform

Send applications to jobs@example.com
"""

RESUME_TXT = """Priya Sharma
Backend Engineer at Example Corp, Pune
Phone: 9876543210 | Email: priya@example.com

Summary
Backend engineer building Python services and LLM applications.

Technical Skill
Programming Language: Python, Java
Web Development Frameworks: FastAPI, Flask
Database: SQL, MySQL
Cloud Technology: AWS

Work History
Backend Engineer | 01-2023 - Current
Example Corp, Pune
- Built Python REST services with FastAPI handling production traffic.
- Designed MySQL schemas and tuned slow queries for reporting workloads.

Junior Developer | 06-2021 - 12-2022
Startup Inc, Pune
- Wrote Flask endpoints and automated data imports in Python.

Education
Bachelor of Engineering: Computer Science | 2017 - 2021
Pune Institute of Technology
"""


@pytest.fixture
def resume_file(tmp_path: Path) -> Path:
    path = tmp_path / "resume.txt"
    path.write_text(RESUME_TXT, encoding="utf-8")
    return path


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh server bound to a throwaway data dir."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("NAUKRI_ENABLED", "false")

    from naukri_mcp.config import get_settings

    get_settings.cache_clear()
    import naukri_mcp.server as server_module

    yield server_module.mcp
    get_settings.cache_clear()


def payload(result) -> dict:
    """Structured content if present, else the parsed text block."""
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_full_flow(server, resume_file: Path):
    async with Client(server, raise_exceptions=True) as client:
        # 1. Import the resume.
        imported = payload(await client.call_tool("import_resume", {"path": str(resume_file)}))
        assert imported["name"] == "Priya Sharma"
        assert imported["counts"]["experience"] == 2
        assert imported["counts"]["experience_bullets"] == 3

        # 2. Add the job by pasting the JD -- no scraping involved.
        job = payload(await client.call_tool("parse_job_description", {"text": JD}))
        job_id = job["job_id"]
        assert job["contact_email"] == "jobs@example.com"

        # 3. Analyze the gap.
        analysis = payload(await client.call_tool("analyze_job_match", {"job_id": job_id}))
        missing = {m["keyword"] for m in analysis["missing_do_not_add"]}
        surfaced = {m["keyword"] for m in analysis["have_surfaced"]}
        adjacent = {m["keyword"] for m in analysis["adjacent"]}

        assert "kubernetes" in missing and "terraform" in missing
        assert "python" in surfaced and "fastapi" in surfaced
        # Has MySQL, not Postgres -> adjacent, never claimed outright.
        assert "postgresql" in adjacent
        assert analysis["score"]["current"] > 0

        # 4a. A fabricated bullet must be refused, and nothing written.
        rejected = payload(
            await client.call_tool(
                "render_resume",
                {
                    "job_id": job_id,
                    "bullets": [
                        {
                            "evidence_ref": "exp.0.bullet.0",
                            "text": "Built Python FastAPI services deployed on Kubernetes.",
                        }
                    ],
                },
            )
        )
        assert rejected["rendered"] is False
        assert rejected["rejected"] is True
        assert any("kubernetes" in e["problem"].lower() for e in rejected["errors"])

        # 4b. An honest rewrite renders.
        rendered = payload(
            await client.call_tool(
                "render_resume",
                {
                    "job_id": job_id,
                    "bullets": [
                        {
                            "evidence_ref": "exp.0.bullet.0",
                            "text": "Built and shipped Python REST APIs with FastAPI in production.",
                            "surfaces": ["python", "fastapi", "rest api"],
                        }
                    ],
                    "summary": "Python backend engineer specializing in FastAPI REST services.",
                    "label": "e2e",
                },
            )
        )
        assert rendered["rendered"] is True
        variant_id = rendered["variant_id"]
        assert Path(rendered["docx"]).exists()
        assert Path(rendered["pdf"]).exists()

        # 5. Email apply returns a draft first -- never sends unprompted.
        draft = payload(
            await client.call_tool(
                "apply_to_job",
                {"job_id": job_id, "variant_id": variant_id, "mode": "email"},
            )
        )
        assert draft["applied"] is False
        assert draft["awaiting_confirmation"] is True
        assert draft["draft"]["to"] == "jobs@example.com"

        # 6. Confirmed send under DRY_RUN transmits nothing but is recorded.
        confirmed = payload(
            await client.call_tool(
                "apply_to_job",
                {
                    "job_id": job_id,
                    "variant_id": variant_id,
                    "mode": "email",
                    "confirm_send": True,
                },
            )
        )
        assert confirmed["dry_run"] is True
        assert confirmed["applied"] is False  # dry run never counts as sent

        # 7. A dry run sent nothing, so the job is still only "saved" and a real apply
        #    must still be possible. The duplicate guard deliberately does not fire here.
        real_apply = payload(
            await client.call_tool("apply_to_job", {"job_id": job_id, "mode": "assisted"})
        )
        assert real_apply["applied"] is True

        # 8. Now that it is genuinely applied, a repeat is refused.
        again = payload(
            await client.call_tool("apply_to_job", {"job_id": job_id, "mode": "assisted"})
        )
        assert again["applied"] is False
        assert again["duplicate"] is True

        # 9. Tracking reflects it.
        listed = payload(await client.call_tool("list_applications", {}))
        assert listed["count"] >= 1
        assert listed["applications"][0]["status"] == "applied"


async def test_assisted_apply_then_duplicate_guard(server, resume_file: Path):
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool("import_resume", {"path": str(resume_file)})
        job = payload(
            await client.call_tool(
                "parse_job_description",
                {"text": "Python Developer\n\nRequired: Python, Flask", "company": "Acme"},
            )
        )
        job_id = job["job_id"]

        first = payload(
            await client.call_tool("apply_to_job", {"job_id": job_id, "mode": "assisted"})
        )
        assert first["applied"] is True
        assert first["mode"] == "assisted"

        second = payload(
            await client.call_tool("apply_to_job", {"job_id": job_id, "mode": "assisted"})
        )
        assert second["applied"] is False
        assert second["duplicate"] is True


async def test_analyze_requires_resume(server):
    """A clear error, not a crash, when the profile is missing."""
    async with Client(server) as client:
        result = await client.call_tool(
            "parse_job_description", {"text": "Python Developer\n\nRequired: Python"}
        )
        job_id = payload(result)["job_id"]
        analysis = await client.call_tool("analyze_job_match", {"job_id": job_id})
        assert analysis.is_error
        assert "import_resume" in analysis.content[0].text


async def test_resource_exposes_evidence_refs(server, resume_file: Path):
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool("import_resume", {"path": str(resume_file)})
        result = await client.read_resource("resume://master")
        profile = json.loads(result.contents[0].text)
        refs = [b["ref"] for e in profile["experience"] for b in e["bullets"]]
        assert "exp.0.bullet.0" in refs
