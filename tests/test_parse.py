"""Resume and JD parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from naukri_mcp.providers.manual import html_to_text, parse_experience, parse_job_text
from naukri_mcp.resume.parse import parse_resume, reflow_lines

RESUME = """Anita Rao
Senior Data Engineer at Globex, Hyderabad
Phone: +91 98765 43210 | Email: anita.rao@example.com

Summary
Data engineer with eight years building batch and streaming pipelines.

Technical Skill
Programming Language: Python, Scala
Data: Spark, Airflow, Kafka
Cloud Technology: AWS, Azure

Work History
Senior Data Engineer | 03-2021 - Current
Globex, Hyderabad
- Built Spark pipelines processing 4TB daily with Airflow orchestration.
- Cut warehouse costs 30% by repartitioning Parquet datasets.

Data Engineer | 07-2018 - 02-2021
Initech, Pune
- Streamed clickstream events through Kafka into Snowflake.

Projects
Realtime Fraud Scoring
Kafka plus Spark Structured Streaming scoring transactions within 200ms.

Education
Bachelor of Technology: Information Technology | 2014 - 2018
COEP Pune
"""


@pytest.fixture
def resume_path(tmp_path: Path) -> Path:
    path = tmp_path / "resume.txt"
    path.write_text(RESUME, encoding="utf-8")
    return path


def test_parses_contact(resume_path):
    profile = parse_resume(resume_path)
    assert profile.contact.name == "Anita Rao"
    assert profile.contact.email == "anita.rao@example.com"
    assert "98765" in profile.contact.phone
    assert profile.contact.location == "Hyderabad"


def test_parses_sections(resume_path):
    profile = parse_resume(resume_path)
    assert profile.summary.startswith("Data engineer with eight years")

    skills = {s.name.lower() for s in profile.skills}
    assert {"python", "scala", "spark", "airflow", "kafka", "aws", "azure"} <= skills

    assert len(profile.experience) == 2
    first = profile.experience[0]
    assert first.title == "Senior Data Engineer"
    assert first.company == "Globex"
    assert first.location == "Hyderabad"
    assert (first.start, first.end) == ("03-2021", "Current")
    assert len(first.bullets) == 2
    assert len(profile.experience[1].bullets) == 1

    assert len(profile.projects) == 1
    assert profile.projects[0].name == "Realtime Fraud Scoring"
    assert profile.education


def test_evidence_refs_are_unique_and_addressable(resume_path):
    profile = parse_resume(resume_path)
    index = profile.evidence_index()
    refs = [b.ref for e in profile.experience for b in e.bullets]
    assert len(refs) == len(set(refs))
    assert all(ref in index for ref in refs)


def test_skill_evidence_links_to_bullets(resume_path):
    """A skill demonstrated in a bullet must be distinguishable from one merely listed."""
    profile = parse_resume(resume_path)
    by_name = {s.name.lower(): s for s in profile.skills}
    assert by_name["spark"].evidenced_by, "Spark appears in a bullet"
    assert not by_name["azure"].evidenced_by, "Azure is only listed, never demonstrated"


def test_unsupported_format_is_rejected_clearly(tmp_path: Path):
    path = tmp_path / "resume.rtf"
    path.write_text("x")
    with pytest.raises(ValueError, match="Unsupported resume format"):
        parse_resume(path)


def test_legacy_doc_gets_actionable_message(tmp_path: Path):
    path = tmp_path / "resume.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(ValueError, match="Save as .docx"):
        parse_resume(path)


def test_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        parse_resume(tmp_path / "nope.pdf")


def test_reflow_joins_wrapped_lines_but_respects_bullets():
    wrapped = [
        "- Built Spark pipelines processing four terabytes of event data every single day",
        "with Airflow orchestration and alerting.",
        "- Cut warehouse costs by thirty percent through repartitioning Parquet datasets",
        "and pruning cold partitions.",
    ]
    out = [ln for ln in reflow_lines(wrapped) if ln.strip()]
    assert len(out) == 2, out
    assert out[0].endswith("alerting.")
    assert out[1].startswith("- Cut warehouse costs")


def test_reflow_leaves_unwrapped_text_alone():
    """No cluster of near-full-width lines means the layout is deliberate."""
    lines = ["Name", "Title", "SUMMARY", "Short line.", "Another short line."]
    assert reflow_lines(lines) == lines


# --- JD parsing ------------------------------------------------------------------


def test_parse_job_text_extracts_fields():
    job = parse_job_text(
        """Job Title: Senior Backend Engineer
        Company: Globex Systems
        Location: Remote, India
        Experience: 4-8 years

        Send your CV to talent@globex.example.com
        """
    )
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Globex Systems"
    assert job.location == "Remote, India"
    assert (job.experience_min, job.experience_max) == (4.0, 8.0)
    assert job.contact_email == "talent@globex.example.com"
    assert job.provider == "manual"


def test_parse_job_text_falls_back_to_first_line_as_title():
    job = parse_job_text("Machine Learning Engineer\n\nWe need Python and PyTorch.")
    assert job.title == "Machine Learning Engineer"


def test_explicit_args_override_detection():
    job = parse_job_text("Job Title: Wrong\nCompany: Wrong", title="Right", company="Also Right")
    assert job.title == "Right"
    assert job.company == "Also Right"


def test_same_jd_yields_stable_id():
    """Re-pasting a JD must not create a duplicate job row."""
    text = "Backend Engineer\n\nPython, FastAPI required."
    assert parse_job_text(text).provider_job_id == parse_job_text(text).provider_job_id


def test_empty_jd_rejected():
    with pytest.raises(ValueError, match="empty"):
        parse_job_text("   \n  ")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3-5 years experience", (3.0, 5.0)),
        ("5+ years", (5.0, None)),
        ("minimum 4 years", (4.0, None)),
        ("2 to 6 yrs", (2.0, 6.0)),
        ("no numbers here", (None, None)),
        ("8-3 years", (3.0, 8.0)),  # reversed range is normalized
    ],
)
def test_experience_parsing(text, expected):
    assert parse_experience(text) == expected


def test_html_to_text_preserves_bullets_and_strips_scripts():
    html = (
        "<div><script>bad()</script><h2>Requirements</h2>"
        "<ul><li>Python 3</li><li>FastAPI &amp; REST</li></ul></div>"
    )
    text = html_to_text(html)
    assert "bad()" not in text
    assert "Requirements" in text
    assert "- Python 3" in text
    assert "FastAPI & REST" in text
