"""JD keyword extraction, synonym normalization, and gap classification.

This module decides what may legitimately be said on a tailored resume. Its output is
consumed by `resume.render.validate_tailoring`, which refuses to render anything that
contradicts it -- so the buckets here are a safety boundary, not a suggestion.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field

from naukri_mcp.resume.profile import MasterProfile


class Bucket(str, Enum):
    have_surfaced = "have_surfaced"
    have_buried = "have_buried"
    adjacent = "adjacent"
    missing = "missing"


#: canonical name -> aliases. Extend freely; matching is whole-word and case-insensitive.
SKILL_VOCAB: dict[str, set[str]] = {
    # languages
    "python": {"python3", "py"},
    "java": set(),
    "javascript": {"js", "ecmascript"},
    "typescript": {"ts"},
    "go": {"golang"},
    "c++": {"cpp"},
    "c#": {"csharp", ".net"},
    "sql": set(),
    "scala": set(),
    # NB: the R language is deliberately absent. A single-letter pattern matches "R&D",
    # "R1", and similar, which would manufacture evidence the candidate does not have.
    "bash": {"shell", "shell scripting"},
    # web / api
    "fastapi": set(),
    "flask": set(),
    "django": set(),
    "spring boot": {"springboot", "spring-boot", "spring"},
    "rest api": {"rest", "restful", "restful api", "rest apis"},
    "graphql": set(),
    "grpc": set(),
    "node.js": {"nodejs", "node"},
    "react": {"react.js", "reactjs"},
    "fastify": set(),
    "uvicorn": set(),
    "gunicorn": set(),
    # data / ml
    "numpy": set(),
    "pandas": set(),
    "scikit-learn": {"sklearn", "scikit learn"},
    "scipy": set(),
    "matplotlib": set(),
    "seaborn": set(),
    "pytorch": {"torch"},
    "tensorflow": {"tf"},
    "keras": set(),
    "xgboost": set(),
    "lightgbm": set(),
    "opencv": set(),
    "hugging face": {"huggingface", "transformers"},
    "spark": {"pyspark", "apache spark"},
    "airflow": {"apache airflow"},
    "dbt": set(),
    "kafka": {"apache kafka"},
    # ml concepts
    "machine learning": {"ml"},
    "deep learning": {"dl", "neural network", "neural networks", "dnn"},
    "nlp": {"natural language processing"},
    "computer vision": {"cv", "image processing"},
    "llm": {"llms", "large language model", "large language models"},
    "rag": {"retrieval augmented generation", "retrieval-augmented generation"},
    "prompt engineering": set(),
    "fine-tuning": {"finetuning", "fine tuning"},
    "mlops": set(),
    "feature engineering": set(),
    "time series": {"timeseries", "forecasting"},
    "recommendation systems": {"recsys", "recommender systems"},
    "generative ai": {"genai", "gen ai"},
    "agentic ai": {"ai agents", "agent based", "autonomous agents", "agentic"},
    "langchain": set(),
    "langgraph": set(),
    "llamaindex": {"llama index"},
    "crewai": {"crew ai"},
    "mcp": {"model context protocol"},
    "graph neural network": {"gnn", "graph neural networks"},
    # data stores
    "postgresql": {"postgres", "psql"},
    "mysql": set(),
    "mongodb": {"mongo"},
    "redis": set(),
    "elasticsearch": {"elastic search", "opensearch"},
    "snowflake": set(),
    "vector database": {"vector db", "vector store", "vectorstore"},
    "chroma": {"chromadb"},
    "faiss": set(),
    "pinecone": set(),
    "weaviate": set(),
    "qdrant": set(),
    # cloud / infra
    "aws": {"amazon web services"},
    "azure": {"microsoft azure"},
    "gcp": {"google cloud", "google cloud platform"},
    "docker": {"containerization", "containers"},
    "kubernetes": {"k8s"},
    "terraform": set(),
    "ci/cd": {"cicd", "continuous integration", "continuous delivery"},
    "jenkins": set(),
    "github actions": set(),
    "sagemaker": {"aws sagemaker"},
    "lambda": {"aws lambda"},
    "bedrock": {"aws bedrock"},
    # practice
    "git": {"version control"},
    "agile": {"scrum"},
    "unit testing": {"pytest", "unit tests", "test driven development", "tdd"},
    "microservices": {"microservice"},
    "system design": {"software architecture"},
    "data visualization": {"dataviz", "visualization"},
    "etl": {"elt", "data pipeline", "data pipelines"},
    "linux": {"unix"},
}

#: Honest neighbours: having the left implies transferable-but-not-identical experience
#: with the right. Used only to frame experience truthfully -- never to claim the skill.
ADJACENT_SKILLS: dict[str, set[str]] = {
    "fastapi": {"flask", "django", "rest api", "spring boot"},
    "flask": {"fastapi", "django", "rest api"},
    "django": {"flask", "fastapi", "rest api"},
    "spring boot": {"java", "rest api"},
    "postgresql": {"mysql", "sql"},
    "mysql": {"postgresql", "sql"},
    "mongodb": {"redis", "vector database"},
    "pytorch": {"tensorflow", "keras", "deep learning"},
    "tensorflow": {"pytorch", "keras", "deep learning"},
    "keras": {"tensorflow", "pytorch"},
    "scikit-learn": {"machine learning", "numpy", "pandas"},
    "langchain": {"langgraph", "llamaindex", "crewai", "llm"},
    "langgraph": {"langchain", "crewai", "agentic ai"},
    "llamaindex": {"langchain", "rag"},
    "crewai": {"langchain", "langgraph", "agentic ai"},
    "rag": {"vector database", "llm", "faiss", "chroma"},
    "vector database": {"chroma", "faiss", "pinecone", "weaviate", "qdrant"},
    "chroma": {"faiss", "pinecone", "vector database"},
    "faiss": {"chroma", "pinecone", "vector database"},
    "pinecone": {"chroma", "faiss", "vector database"},
    "kubernetes": {"docker"},
    "docker": {"kubernetes"},
    "gcp": {"aws", "azure"},
    "azure": {"aws", "gcp"},
    "aws": {"azure", "gcp"},
    "spark": {"pandas", "etl"},
    "airflow": {"etl"},
    "typescript": {"javascript"},
    "javascript": {"typescript"},
    "go": {"python", "java"},
    "computer vision": {"deep learning", "opencv"},
    "nlp": {"llm", "deep learning", "hugging face"},
    "llm": {"nlp", "generative ai", "hugging face"},
    "generative ai": {"llm", "agentic ai"},
    "agentic ai": {"llm", "langgraph", "langchain"},
    "mlops": {"ci/cd", "docker"},
}

#: Entailment, specific -> the general skills it genuinely proves. Distinct from
#: ADJACENT_SKILLS, which is a *sibling* relation: having Chroma does not mean you have
#: used Pinecone, but having FastAPI does mean you have built REST APIs. Sibling
#: adjacency may only be alluded to; entailment may be stated outright, because it is
#: true. Conflating the two made the analyzer report "REST API" as a required gap for a
#: candidate with three REST frameworks on their resume.
IMPLIES: dict[str, set[str]] = {
    "fastapi": {"rest api"},
    "flask": {"rest api"},
    "django": {"rest api"},
    "spring boot": {"rest api"},
    "pytorch": {"deep learning"},
    "tensorflow": {"deep learning"},
    "keras": {"deep learning"},
    "deep learning": {"machine learning"},
    "scikit-learn": {"machine learning"},
    "xgboost": {"machine learning"},
    "lightgbm": {"machine learning"},
    "langchain": {"llm"},
    "langgraph": {"llm", "agentic ai"},
    "llamaindex": {"llm", "rag"},
    "crewai": {"llm", "agentic ai"},
    "llm": {"generative ai", "nlp"},
    "chroma": {"vector database"},
    "faiss": {"vector database"},
    "pinecone": {"vector database"},
    "weaviate": {"vector database"},
    "qdrant": {"vector database"},
    "postgresql": {"sql"},
    "mysql": {"sql"},
    "github actions": {"ci/cd"},
    "jenkins": {"ci/cd"},
    "kubernetes": {"docker"},
    "airflow": {"etl"},
    "spark": {"etl"},
    "opencv": {"computer vision"},
    "hugging face": {"nlp"},
    "pytest": {"unit testing"},
}

#: general -> specifics that prove it.
_IMPLIED_BY: dict[str, set[str]] = {}
for _specific, _generals in IMPLIES.items():
    for _general in _generals:
        _IMPLIED_BY.setdefault(_general, set()).add(_specific)


def _symmetric_closure(pairs: dict[str, set[str]]) -> dict[str, set[str]]:
    """Adjacency is mutually transferable, so the map must be symmetric.

    Hand-maintaining both directions is error-prone -- an early version listed
    fastapi -> rest api but not the reverse, so a JD requiring "REST API" scored the
    candidate as missing it despite three REST frameworks on their resume.
    """
    out: dict[str, set[str]] = {k: set(v) for k, v in pairs.items()}
    for left, rights in pairs.items():
        for right in rights:
            out.setdefault(right, set()).add(left)
    for key in out:
        out[key].discard(key)
    return out


ADJACENT_SKILLS = _symmetric_closure(ADJACENT_SKILLS)


#: Sections that signal a hard requirement rather than a nice-to-have.
REQUIRED_CUES = re.compile(
    r"\b(must[- ]have|required|requirement|essential|mandatory|minimum qualification"
    r"|you (?:will )?need|we require)\b",
    re.I,
)
PREFERRED_CUES = re.compile(
    r"\b(nice[- ]to[- ]have|preferred|plus|bonus|good to have|desirable|advantage)\b", re.I
)

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canon, _aliases in SKILL_VOCAB.items():
    _ALIAS_TO_CANONICAL[_canon] = _canon
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias] = _canon


def _word_pattern(term: str) -> re.Pattern[str]:
    """Whole-token match that tolerates the punctuation in names like c++ / ci-cd / node.js.

    The trailing guard rejects a following alphanumeric, `+`, or `#` (so "go" does not
    match "golang" and "c" does not match "c++"), and rejects a dot only when the dot is
    itself followed by an alphanumeric (so "node" does not match inside "node.js").

    That last distinction matters more than it looks: an earlier version excluded a bare
    trailing dot too, which meant a keyword in sentence-final position -- "deployed on
    Kubernetes." -- silently failed to match, and the fabrication validator let it
    through. tests/test_validator.py covers exactly that case.
    """
    escaped = re.escape(term).replace(r"\ ", r"[\s\-_]+")
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9+#])(?!\.[A-Za-z0-9])", re.I)


_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    canon: [_word_pattern(t) for t in ({canon} | aliases)]
    for canon, aliases in SKILL_VOCAB.items()
}


def find_skills(text: str) -> set[str]:
    """Canonical skills mentioned anywhere in `text`."""
    if not text:
        return set()
    return {canon for canon, pats in _PATTERNS.items() if any(p.search(text) for p in pats)}


class KeywordFinding(BaseModel):
    keyword: str
    bucket: Bucket
    #: Why we placed it here -- the resume text that proves it, if any.
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_text: str = ""
    #: For `adjacent`: which skills you do have that are related.
    related_have: list[str] = Field(default_factory=list)
    #: Specific skills of yours that genuinely entail this one (FastAPI -> REST API).
    #: Present means you may state this keyword outright; it is not a gap.
    implied_by: list[str] = Field(default_factory=list)
    required: bool = False
    weight: float = 1.0
    #: Listed among your skills but never demonstrated in an achievement bullet. Passes
    #: keyword screens, but is the weak spot an interviewer will probe.
    weak_claim: bool = False

    @property
    def action(self) -> str:
        base = {
            Bucket.have_surfaced: "Already prominent -- leave as is.",
            Bucket.have_buried: "Promote: reword an existing bullet to lead with this.",
            Bucket.adjacent: "Frame honestly via the related skill. Never claim this outright.",
            Bucket.missing: "DO NOT add to the resume. Learning target only.",
        }[self.bucket]
        if self.implied_by:
            base += (
                f" You can state this outright: {', '.join(self.implied_by)} proves it."
            )
        if self.weak_claim:
            base += " You list it but never show it working -- consider adding evidence."
        return base


class GapReport(BaseModel):
    job_title: str = ""
    job_company: str = ""
    findings: list[KeywordFinding] = Field(default_factory=list)
    unknown_terms: list[str] = Field(default_factory=list)

    def by_bucket(self, bucket: Bucket) -> list[KeywordFinding]:
        return [f for f in self.findings if f.bucket == bucket]

    @property
    def forbidden_keywords(self) -> set[str]:
        """Keywords the renderer must refuse to let a tailored bullet assert."""
        return {f.keyword for f in self.findings if f.bucket == Bucket.missing}

    @property
    def promotable(self) -> list[KeywordFinding]:
        return self.by_bucket(Bucket.have_buried)


def _requirement_weight(text: str, keyword: str) -> tuple[bool, float]:
    """Infer whether a keyword is required, from the sentence it appears in."""
    for pat in _PATTERNS.get(keyword, []):
        for m in pat.finditer(text):
            # Look at the enclosing line plus the line above (headings like "Must have:").
            line_start = text.rfind("\n", 0, m.start())
            prev_start = text.rfind("\n", 0, max(line_start, 0))
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            context = text[max(prev_start, 0) : line_end]
            if PREFERRED_CUES.search(context):
                return False, 0.5
            if REQUIRED_CUES.search(context):
                return True, 1.5
    return False, 1.0


def build_gap_report(
    profile: MasterProfile,
    jd_text: str,
    *,
    job_title: str = "",
    job_company: str = "",
) -> GapReport:
    """Classify every JD keyword against the profile."""
    jd_skills = find_skills(jd_text)
    if job_title:
        jd_skills |= find_skills(job_title)

    have = find_skills(profile.searchable_text())

    # The "top zone" is what a recruiter skimming for 10 seconds and an ATS keyword
    # scanner both see first: summary, the skills block, and the current role.
    top_zone = " ".join(
        [profile.summary, *(s.name for s in profile.skills)]
        + [exp.title for exp in profile.experience[:2]]
        + [b.text for exp in profile.experience[:1] for b in exp.bullets]
    )
    prominent = find_skills(top_zone)

    # Demonstrated = shown working in an achievement bullet or project, as opposed to
    # merely listed in the skills block.
    demonstrated_text = " ".join(
        [b.text for exp in profile.experience for b in exp.bullets]
        + [f"{p.name} {p.description} {' '.join(p.tech)}" for p in profile.projects]
        + [b.text for p in profile.projects for b in p.bullets]
    )
    demonstrated = find_skills(demonstrated_text)

    evidence_map = _build_evidence_map(profile)

    findings: list[KeywordFinding] = []
    for keyword in sorted(jd_skills):
        required, weight = _requirement_weight(jd_text, keyword)
        refs, ev_text = evidence_map.get(keyword, ([], ""))

        if keyword in have:
            bucket = Bucket.have_surfaced if keyword in prominent else Bucket.have_buried
            findings.append(
                KeywordFinding(
                    keyword=keyword,
                    bucket=bucket,
                    evidence_refs=refs,
                    evidence_text=ev_text,
                    required=required,
                    weight=weight,
                    weak_claim=keyword not in demonstrated,
                )
            )
            continue

        # Entailed by something they demonstrably have: not a gap, and safe to state.
        provers = sorted(_IMPLIED_BY.get(keyword, set()) & have)
        if provers:
            prover_refs: list[str] = []
            prover_text = ""
            for prover in provers:
                p_refs, p_text = evidence_map.get(prover, ([], ""))
                prover_refs.extend(p_refs)
                prover_text = prover_text or p_text
            findings.append(
                KeywordFinding(
                    keyword=keyword,
                    bucket=(
                        Bucket.have_surfaced
                        if any(p in prominent for p in provers)
                        else Bucket.have_buried
                    ),
                    evidence_refs=prover_refs[:6],
                    evidence_text=prover_text,
                    implied_by=provers,
                    required=required,
                    weight=weight,
                    weak_claim=not any(p in demonstrated for p in provers),
                )
            )
            continue

        related = sorted(ADJACENT_SKILLS.get(keyword, set()) & have)
        findings.append(
            KeywordFinding(
                keyword=keyword,
                bucket=Bucket.adjacent if related else Bucket.missing,
                related_have=related,
                required=required,
                weight=weight,
            )
        )

    return GapReport(
        job_title=job_title,
        job_company=job_company,
        findings=findings,
        unknown_terms=sorted(extract_unknown_terms(jd_text) - jd_skills),
    )


def _build_evidence_map(profile: MasterProfile) -> dict[str, tuple[list[str], str]]:
    """canonical skill -> (refs proving it, one representative snippet)."""
    out: dict[str, tuple[list[str], str]] = {}
    candidates: list[tuple[str, str]] = []
    for skill in profile.skills:
        candidates.append((skill.ref, skill.name))
    for exp in profile.experience:
        for bullet in exp.bullets:
            candidates.append((bullet.ref, bullet.text))
    for proj in profile.projects:
        if proj.description or proj.tech:
            candidates.append((proj.ref, f"{proj.name} {proj.description} {' '.join(proj.tech)}"))
        for bullet in proj.bullets:
            candidates.append((bullet.ref, bullet.text))
    if profile.summary:
        candidates.append(("summary", profile.summary))

    for canon, pats in _PATTERNS.items():
        refs: list[str] = []
        snippet = ""
        for ref, text in candidates:
            if any(p.search(text) for p in pats):
                refs.append(ref)
                # Prefer a real achievement bullet over a bare skills-list mention.
                if not snippet or (ref.startswith(("exp.", "proj.")) and "." in ref):
                    snippet = text.strip()[:300]
        if refs:
            out[canon] = (refs, snippet)
    return out


#: Titlecase term, optionally two words. Uses [ \t] rather than \s so a match never
#: straddles a line break and swallows the next heading.
_TITLECASE_TERM = re.compile(
    r"\b(?:[A-Z][a-zA-Z0-9+#.]{1,18}(?:[ \t]+[A-Z][a-zA-Z0-9+#.]{1,18})?)\b"
)
_STOPWORDS = {
    "the", "and", "for", "with", "you", "we", "our", "your", "will", "must", "have",
    "years", "year", "experience", "job", "role", "team", "work", "working", "good",
    "strong", "knowledge", "skills", "skill", "ability", "responsibilities", "required",
    "requirements", "candidate", "company", "please", "apply", "location", "salary",
    "notice", "period", "immediate", "joiner", "full", "time", "position", "description",
    "qualifications", "preferred", "plus", "bonus", "about", "us", "who", "what", "why",
}


def extract_unknown_terms(jd_text: str, *, limit: int = 25) -> set[str]:
    """Capitalized terms we have no vocabulary entry for.

    Reported separately so you can spot a technology worth adding to SKILL_VOCAB. These
    are never auto-classified -- an unknown term is not evidence of anything.
    """
    counts: dict[str, int] = {}
    for m in _TITLECASE_TERM.finditer(jd_text):
        term = m.group(0).strip()
        low = term.lower()
        if low in _STOPWORDS or len(low) < 3 or low in _ALIAS_TO_CANONICAL:
            continue
        if any(w in _STOPWORDS for w in low.split()):
            continue
        # Skip sentence- and bullet-initial words: capitalization there is grammar, not
        # a proper noun, so "Build ..." and "Collaborate ..." are not technologies.
        line_start = jd_text.rfind("\n", 0, m.start()) + 1
        if not jd_text[line_start : m.start()].strip(" \t-–—*•·>.)(1234567890"):
            continue
        counts[term] = counts.get(term, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {term for term, _ in ranked[:limit]}
