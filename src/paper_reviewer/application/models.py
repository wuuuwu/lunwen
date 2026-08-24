from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from paper_reviewer.domain.document import DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.domain.review import (
    EvaluationReport,
    HardRuleAssessment,
    HumanRuleDecision,
    MetaReview,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.validation.audits import AuditReport


class ReportExportFormat(StrEnum):
    MARKDOWN = "markdown"
    PDF = "pdf"


class ReportExportResult(BaseModel):
    path: Path
    format: ReportExportFormat
    size_bytes: int = Field(ge=0)
    reconstructed_from_snapshot: bool = False


class ReviewRequest(BaseModel):
    paper: Path
    provider: str
    model: str
    rubric: Path
    profile: Path
    discipline_name: str
    discipline_profile: Path | None = None
    cloud_processing_authorized: bool = False
    contains_classified_material: bool = False
    external_search: bool = True

    @field_validator("discipline_name")
    @classmethod
    def validate_discipline_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("discipline_name is required")
        return normalized


class RubricValidationResult(BaseModel):
    valid: bool
    rubric: RubricProfile | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    weight_total: float = 0
    profile_compatible: bool = False


class RunEvent(BaseModel):
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str
    status: RunStatus | None = None
    stage: str | None = None
    message: str
    payload: dict[str, object] = Field(default_factory=dict)


class RunSummary(BaseModel):
    run_id: str
    paper_name: str
    rubric_id: str
    provider: str
    model: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    error: str | None = None

    @classmethod
    def from_record(cls, run: RunRecord) -> RunSummary:
        return cls(
            run_id=run.run_id,
            paper_name=Path(run.input_path).name,
            rubric_id=run.rubric_id,
            provider=run.provider,
            model=run.model,
            status=run.status,
            created_at=run.created_at,
            updated_at=run.updated_at,
            error=run.error,
        )


class RunDetail(BaseModel):
    run: RunRecord
    events: list[RunEvent] = Field(default_factory=list)
    pending_hard_rules: list[HardRuleAssessment] = Field(default_factory=list)
    human_rule_decisions: list[HumanRuleDecision] = Field(default_factory=list)


class ReportView(BaseModel):
    run: RunRecord
    document: DocumentInfo | None = None
    rubric: RubricProfile
    review: MetaReview
    audit: AuditReport
    evidence: list[EvidenceItem] = Field(default_factory=list)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    report_markdown: Path
    report_json: Path
    evaluation: EvaluationReport | None = None
