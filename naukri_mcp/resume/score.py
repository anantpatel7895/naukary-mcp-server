"""Explainable ATS match scoring.

Every score comes with the per-keyword contributions that produced it. A number you
cannot audit is worse than no number, because it invites tailoring toward the metric.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from naukri_mcp.resume.keywords import Bucket, GapReport

#: How much credit each bucket earns. `adjacent` earns partial credit because a
#: recruiter reading the resume will recognize the transferable skill even though an
#: exact-match keyword filter will not.
BUCKET_CREDIT: dict[Bucket, float] = {
    Bucket.have_surfaced: 1.0,
    Bucket.have_buried: 0.55,
    Bucket.adjacent: 0.25,
    Bucket.missing: 0.0,
}

#: Credit a buried keyword reaches once promoted into a prominent bullet.
PROMOTED_CREDIT = BUCKET_CREDIT[Bucket.have_surfaced]


class KeywordContribution(BaseModel):
    keyword: str
    bucket: Bucket
    weight: float
    credit: float
    points: float
    required: bool


class MatchScore(BaseModel):
    score: float = Field(description="0-100 weighted keyword coverage as the resume stands.")
    potential_score: float = Field(
        description="0-100 achievable by promoting buried keywords only -- no fabrication."
    )
    max_points: float
    earned_points: float
    contributions: list[KeywordContribution] = Field(default_factory=list)
    required_missing: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)

    @property
    def headline(self) -> str:
        gain = self.potential_score - self.score
        if gain >= 1:
            return (
                f"{self.score:.0f}/100 now, {self.potential_score:.0f}/100 achievable "
                f"by surfacing what you already have (+{gain:.0f})."
            )
        return f"{self.score:.0f}/100. No honest gain left from reordering alone."


def score_match(report: GapReport) -> MatchScore:
    contributions: list[KeywordContribution] = []
    max_points = 0.0
    earned = 0.0
    potential = 0.0

    for finding in report.findings:
        credit = BUCKET_CREDIT[finding.bucket]
        points = finding.weight * credit
        max_points += finding.weight
        earned += points
        # Promoting a buried keyword is the only honest improvement available.
        potential += finding.weight * (
            PROMOTED_CREDIT if finding.bucket == Bucket.have_buried else credit
        )
        contributions.append(
            KeywordContribution(
                keyword=finding.keyword,
                bucket=finding.bucket,
                weight=finding.weight,
                credit=credit,
                points=round(points, 3),
                required=finding.required,
            )
        )

    def pct(value: float) -> float:
        return round(100.0 * value / max_points, 1) if max_points else 0.0

    counts: dict[str, int] = {b.value: 0 for b in Bucket}
    for finding in report.findings:
        counts[finding.bucket.value] += 1

    return MatchScore(
        score=pct(earned),
        potential_score=pct(potential),
        max_points=round(max_points, 3),
        earned_points=round(earned, 3),
        # Worst offenders first: required gaps, then heaviest weight.
        contributions=sorted(
            contributions, key=lambda c: (-c.required, c.credit, -c.weight, c.keyword)
        ),
        required_missing=[
            f.keyword
            for f in report.findings
            if f.required and f.bucket in (Bucket.missing, Bucket.adjacent)
        ],
        counts=counts,
    )
