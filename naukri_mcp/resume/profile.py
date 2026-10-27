"""The MasterProfile: the single source of truth about the candidate.

Everything the server ever puts on a rendered resume must trace back to an entry in
here via a stable `ref` id. That traceability is what makes the anti-fabrication
guarantee enforceable rather than aspirational -- see `resume.render.validate_tailoring`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["experience_bullet", "skill", "project_bullet", "summary", "education"]


class Contact(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


class Bullet(BaseModel):
    """One achievement line, addressable by `ref`."""

    ref: str = Field(description="Stable id, e.g. 'exp.0.bullet.2'. Used as evidence_ref.")
    text: str


class Experience(BaseModel):
    ref: str
    company: str
    title: str
    start: str = ""
    end: str = ""
    location: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    ref: str
    name: str
    description: str = ""
    tech: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class Education(BaseModel):
    ref: str
    institution: str
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    details: str = ""


class Skill(BaseModel):
    ref: str
    name: str
    category: str = ""
    #: Refs of bullets that demonstrate this skill in practice. Empty means the skill
    #: is only *claimed* in a skills list, which is weaker evidence but still evidence.
    evidenced_by: list[str] = Field(default_factory=list)


class MasterProfile(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    summary: str = ""
    skills: list[Skill] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    #: Raw extracted text, kept so keyword matching can fall back to the full document
    #: when the structural parse misses a section.
    raw_text: str = ""
    source_file: str = ""

    # --- evidence lookup -----------------------------------------------------

    def evidence_index(self) -> dict[str, tuple[EvidenceKind, str]]:
        """Map every addressable ref -> (kind, text).

        This is the allow-list the tailoring validator checks proposed bullets against.
        """
        index: dict[str, tuple[EvidenceKind, str]] = {}
        if self.summary:
            index["summary"] = ("summary", self.summary)
        for skill in self.skills:
            index[skill.ref] = ("skill", skill.name)
        for exp in self.experience:
            header = f"{exp.title} at {exp.company}"
            index[exp.ref] = ("experience_bullet", header)
            for bullet in exp.bullets:
                index[bullet.ref] = ("experience_bullet", bullet.text)
        for proj in self.projects:
            index[proj.ref] = ("project_bullet", f"{proj.name}: {proj.description}")
            for bullet in proj.bullets:
                index[bullet.ref] = ("project_bullet", bullet.text)
        for edu in self.education:
            index[edu.ref] = ("education", f"{edu.degree} {edu.field} {edu.institution}".strip())
        return index

    def searchable_text(self) -> str:
        """All candidate-owned text, for keyword presence checks."""
        parts: list[str] = [self.summary]
        parts.extend(s.name for s in self.skills)
        for exp in self.experience:
            parts.extend([exp.title, exp.company, *(b.text for b in exp.bullets)])
        for proj in self.projects:
            parts.extend([proj.name, proj.description, *proj.tech,
                          *(b.text for b in proj.bullets)])
        for edu in self.education:
            parts.extend([edu.degree, edu.field, edu.institution, edu.details])
        parts.extend(self.certifications)
        # raw_text is the safety net for anything the structural parse dropped.
        parts.append(self.raw_text)
        return "\n".join(p for p in parts if p)

    def is_empty(self) -> bool:
        return not (self.experience or self.skills or self.projects or self.raw_text)
