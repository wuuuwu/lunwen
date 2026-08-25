from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from paper_reviewer.application.review_planner import ReviewPlan
from paper_reviewer.config import ReviewProfile
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.domain.reference import ReferenceCheckReport
from paper_reviewer.domain.review import EvaluationReport, MetaReview, ReviewerResult
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord
from paper_reviewer.validation.audits import AuditReport


@dataclass(slots=True)
class PipelineContext:
    """Mutable state shared by the explicitly ordered review pipeline stages.

    This is deliberately a data carrier rather than a generic stage framework:
    stage order, checkpoints, and error boundaries remain owned by the
    orchestrator.
    """

    run: RunRecord
    run_dir: Path
    rubric: RubricProfile
    panel_profile: ReviewProfile | None
    plan: ReviewPlan
    dual_advisory: bool
    discipline_name: str
    discipline_profile: str | None
    document: DocumentInfo | None = None
    blocks: list[DocumentBlock] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    reference_report: ReferenceCheckReport = field(default_factory=ReferenceCheckReport)
    results: list[ReviewerResult] = field(default_factory=list)
    audit: AuditReport | None = None
    meta: MetaReview | None = None
    evaluation: EvaluationReport | None = None
