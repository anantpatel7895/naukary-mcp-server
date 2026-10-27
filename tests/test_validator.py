"""Anti-fabrication guarantee.

If any test in this file fails, the server is unsafe to use: it means a tailored resume
could assert something the candidate's real resume does not support.
"""

from __future__ import annotations

import pytest

from naukri_mcp.resume.keywords import Bucket, build_gap_report
from naukri_mcp.resume.profile import (
    Bullet,
    Contact,
    Experience,
    MasterProfile,
    Project,
    Skill,
)
from naukri_mcp.resume.render import (
    TailoredBullet,
    TailoringProposal,
    TailoringRejected,
    apply_proposal,
    validate_tailoring,
)


@pytest.fixture
def profile() -> MasterProfile:
    """A candidate with Python/FastAPI/LangChain but no Kubernetes and no Terraform."""
    return MasterProfile(
        contact=Contact(name="Test Candidate", email="t@example.com"),
        summary="Backend engineer working on LLM applications.",
        skills=[
            Skill(ref="skill.python", name="Python", category="Language"),
            Skill(ref="skill.fastapi", name="FastAPI", category="Web"),
            Skill(ref="skill.langchain", name="LangChain", category="AI"),
            Skill(ref="skill.faiss", name="FAISS", category="Vector DB"),
        ],
        experience=[
            Experience(
                ref="exp.0",
                company="Acme",
                title="Backend Engineer",
                bullets=[
                    Bullet(ref="exp.0.bullet.0", text="Built REST services in Python with FastAPI."),
                    Bullet(ref="exp.0.bullet.1", text="Shipped a LangChain retrieval pipeline over FAISS."),
                ],
            )
        ],
        projects=[
            Project(ref="proj.0", name="Doc search", description="FAISS vector search demo.")
        ],
        raw_text="Python FastAPI LangChain FAISS REST",
    )


@pytest.fixture
def jd() -> str:
    return """
    Senior Platform Engineer

    Must have:
    - Python and FastAPI
    - Kubernetes for orchestration
    - Terraform for infrastructure as code
    """


def test_missing_keyword_is_bucketed_missing(profile, jd):
    report = build_gap_report(profile, jd)
    buckets = {f.keyword: f.bucket for f in report.findings}
    assert buckets["kubernetes"] == Bucket.missing
    assert buckets["terraform"] == Bucket.missing
    assert buckets["python"] == Bucket.have_surfaced
    assert buckets["fastapi"] == Bucket.have_surfaced


def test_rejects_bullet_asserting_missing_keyword(profile, jd):
    """The core guarantee: you cannot add a skill you do not have."""
    report = build_gap_report(profile, jd)
    proposal = TailoringProposal(
        bullets=[
            TailoredBullet(
                evidence_ref="exp.0.bullet.0",
                text="Built REST services in Python with FastAPI, deployed on Kubernetes.",
            )
        ]
    )
    result = validate_tailoring(proposal, profile, report)
    assert not result.ok
    assert any("kubernetes" in e.problem.lower() for e in result.errors)

    with pytest.raises(TailoringRejected, match="not supported by your resume"):
        result.raise_for_status()


def test_rejects_bullet_without_evidence_ref(profile, jd):
    report = build_gap_report(profile, jd)
    proposal = TailoringProposal(
        bullets=[TailoredBullet(evidence_ref="", text="Led a team of ten engineers.")]
    )
    result = validate_tailoring(proposal, profile, report)
    assert not result.ok
    assert any("evidence_ref" in e.problem for e in result.errors)


def test_rejects_bullet_with_unknown_evidence_ref(profile, jd):
    report = build_gap_report(profile, jd)
    proposal = TailoringProposal(
        bullets=[
            TailoredBullet(evidence_ref="exp.9.bullet.9", text="Built something in Python.")
        ]
    )
    result = validate_tailoring(proposal, profile, report)
    assert not result.ok
    assert any("does not exist in your resume" in e.problem for e in result.errors)


def test_rejects_summary_asserting_missing_keyword(profile, jd):
    report = build_gap_report(profile, jd)
    proposal = TailoringProposal(summary="Platform engineer specializing in Terraform.")
    result = validate_tailoring(proposal, profile, report)
    assert not result.ok
    assert any(e.where == "summary" for e in result.errors)


def test_rejects_adjacent_keyword_asserted_as_owned(profile):
    """Having FAISS makes Pinecone adjacent -- adjacent may be alluded to, not claimed."""
    report = build_gap_report(profile, "We need production Pinecone experience.")
    assert {f.keyword: f.bucket for f in report.findings}["pinecone"] == Bucket.adjacent

    proposal = TailoringProposal(
        bullets=[
            TailoredBullet(
                evidence_ref="exp.0.bullet.1",
                text="Shipped a retrieval pipeline over Pinecone.",
            )
        ]
    )
    result = validate_tailoring(proposal, profile, report)
    assert not result.ok
    assert any("adjacent" in e.problem for e in result.errors)


def test_entailed_keyword_is_not_a_gap_and_may_be_stated(profile):
    """FastAPI genuinely proves REST API experience, so claiming it is true, not fabrication.

    This is the distinction between entailment (specific proves general) and sibling
    adjacency (Chroma does not prove Pinecone). Collapsing the two made the analyzer
    report REST API as a required gap for a candidate with three REST frameworks.
    """
    # A profile that never says "REST" anywhere, so the only route to it is entailment.
    no_rest = profile.model_copy(deep=True)
    no_rest.raw_text = "Python FastAPI LangChain FAISS"
    no_rest.experience[0].bullets[0].text = "Built HTTP services in Python with FastAPI."

    report = build_gap_report(no_rest, "Required: strong REST API development experience.")
    finding = {f.keyword: f for f in report.findings}["rest api"]
    assert finding.bucket in (Bucket.have_surfaced, Bucket.have_buried)
    assert "fastapi" in finding.implied_by
    assert "rest api" not in report.forbidden_keywords
    # Evidence points at the FastAPI bullet that proves it.
    assert finding.evidence_refs

    proposal = TailoringProposal(
        bullets=[
            TailoredBullet(
                evidence_ref="exp.0.bullet.0",
                text="Built and shipped REST APIs in Python using FastAPI.",
            )
        ]
    )
    assert validate_tailoring(proposal, no_rest, report).ok


def test_sibling_adjacency_is_still_blocked_after_entailment_change(profile):
    """The entailment path must not weaken the sibling rule."""
    report = build_gap_report(profile, "Required: Pinecone in production.")
    finding = {f.keyword: f for f in report.findings}["pinecone"]
    assert finding.bucket == Bucket.adjacent
    assert not finding.implied_by


def test_rejects_new_skill_via_skill_reordering(profile, jd):
    report = build_gap_report(profile, jd)
    proposal = TailoringProposal(skill_order=["Python", "Kubernetes"])
    result = validate_tailoring(proposal, profile, report)
    assert not result.ok
    assert any(e.where == "skill_order" for e in result.errors)


def test_accepts_honest_promotion(profile, jd):
    """Rewording to surface a skill you demonstrably have must pass."""
    report = build_gap_report(profile, jd)
    proposal = TailoringProposal(
        summary="Python backend engineer building FastAPI services for LLM retrieval.",
        bullets=[
            TailoredBullet(
                evidence_ref="exp.0.bullet.0",
                text="Designed and shipped Python REST APIs with FastAPI serving production traffic.",
                surfaces=["python", "fastapi"],
            )
        ],
        skill_order=["Python", "FastAPI", "LangChain", "FAISS"],
    )
    result = validate_tailoring(proposal, profile, report)
    assert result.ok, [e.model_dump() for e in result.errors]
    assert "fastapi" in result.surfaced_keywords
    result.raise_for_status()  # must not raise


def test_apply_proposal_replaces_only_cited_bullets(profile):
    proposal = TailoringProposal(
        bullets=[TailoredBullet(evidence_ref="exp.0.bullet.0", text="NEW TEXT")]
    )
    tailored = apply_proposal(profile, proposal)
    assert tailored.experience[0].bullets[0].text == "NEW TEXT"
    # The uncited bullet is untouched...
    assert tailored.experience[0].bullets[1].text == profile.experience[0].bullets[1].text
    # ...and the original profile is not mutated.
    assert profile.experience[0].bullets[0].text != "NEW TEXT"


def test_apply_proposal_reorders_skills(profile):
    proposal = TailoringProposal(skill_order=["FAISS", "LangChain", "FastAPI", "Python"])
    tailored = apply_proposal(profile, proposal)
    assert [s.name for s in tailored.skills] == ["FAISS", "LangChain", "FastAPI", "Python"]


def test_validation_reports_every_problem_at_once(profile, jd):
    """A partial report would let a second violation slip through on the next attempt."""
    report = build_gap_report(profile, jd)
    proposal = TailoringProposal(
        bullets=[
            TailoredBullet(evidence_ref="", text="Ran Kubernetes clusters."),
            TailoredBullet(evidence_ref="nope", text="Wrote Terraform modules."),
        ]
    )
    result = validate_tailoring(proposal, profile, report)
    assert not result.ok
    assert len(result.errors) >= 4  # missing ref, bad ref, kubernetes, terraform
