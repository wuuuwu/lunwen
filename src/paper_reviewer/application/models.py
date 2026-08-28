from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from paper_reviewer.domain.document import DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.domain.provider import ModelApiProtocol
from paper_reviewer.domain.review import (
    EvaluationReport,
    HardRuleAssessment,
    HumanPanelDecision,
    HumanReviewSummary,
    HumanRuleDecision,
    MetaReview,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.domain.submission import SubmissionFieldEvidence, SubmissionMetadata
from paper_reviewer.reporting.presentation import ReportPresentationProfile
from paper_reviewer.validation.audits import AuditReport


class ReportExportFormat(StrEnum):
    MARKDOWN = "markdown"
    PDF = "pdf"


class ReportExportResult(BaseModel):
    path: Path
    format: ReportExportFormat
    size_bytes: int = Field(ge=0)
    reconstructed_from_snapshot: bool = False


MetadataFieldName = Literal["student_name", "student_id", "major", "paper_title"]


class MetadataFieldSuggestion(BaseModel):
    field: MetadataFieldName
    current_value: str
    suggested_value: str
    evidence: SubmissionFieldEvidence
    reason: str
    selected_by_default: bool = True


class BatchMetadataRecheckItem(BaseModel):
    item_id: str
    source_filename: str
    base_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    suggestions: list[MetadataFieldSuggestion]
    unresolved_fields: list[MetadataFieldName] = Field(default_factory=list)


class BatchMetadataRecheckPreview(BaseModel):
    batch_id: str
    items: list[BatchMetadataRecheckItem]
    skipped: dict[str, str] = Field(default_factory=dict)


class MetadataRecheckDecision(BaseModel):
    item_id: str
    base_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: dict[MetadataFieldName, str]
    accepted_fields: list[MetadataFieldName]
    # A recheck decision must never silently turn into an acknowledgement when
    # it is deserialized from a partial payload.  Callers that intend to mark
    # the metadata as checked must explicitly pass ``human_reviewed=True``.
    human_reviewed: bool = False

    @property
    def has_explicit_human_review_confirmation(self) -> bool:
        """Whether this decision explicitly opted into human confirmation.

        The service layer uses this distinction at its trust boundary.  A
        missing field is different from a deliberate acknowledgement, even
        though both are represented by ``False``/``True`` values after
        validation.
        """

        return "human_reviewed" in self.model_fields_set and self.human_reviewed is True


class BatchMetadataRecheckResult(BaseModel):
    batch_id: str
    updated_item_ids: list[str]
    failed_items: dict[str, str]
    skipped_item_ids: list[str] = Field(default_factory=list)


class ProviderErrorDetails(BaseModel):
    """Whitelisted, display-safe fields returned by a Provider."""

    message: str | None = None
    code: str | None = None
    param: str | None = None


class ProviderResponseDiagnostics(BaseModel):
    """Content-free metadata from a compatibility probe response."""

    response_status: str | None = None
    incomplete_reason: str | None = None
    finish_reason: str | None = None
    output_item_types: list[str] = Field(default_factory=list)
    plain_text_only: bool = False


class ProviderCompatibilityResult(BaseModel):
    compatible: bool
    message: str
    protocol: ModelApiProtocol
    error_details: ProviderErrorDetails | None = None
    response_diagnostics: ProviderResponseDiagnostics | None = None


class ReviewRequest(BaseModel):
    paper: Path
    provider: str
    model: str
    rubric: Path
    profile: Path
    discipline_name: str = ""
    discipline_profile: Path | None = None
    cloud_processing_authorized: bool = False
    contains_classified_material: bool = False
    external_search: bool = True

    @field_validator("discipline_name")
    @classmethod
    def validate_discipline_name(cls, value: str) -> str:
        return value.strip()


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
    provider_display_name: str | None = None
    provider_protocol: ModelApiProtocol | None = None
    model: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    error: str | None = None

    @classmethod
    def from_record(
        cls,
        run: RunRecord,
        *,
        provider_display_name: str | None = None,
        provider_protocol: ModelApiProtocol | None = None,
    ) -> RunSummary:
        return cls(
            run_id=run.run_id,
            paper_name=Path(run.input_path).name,
            rubric_id=run.rubric_id,
            provider=run.provider,
            provider_display_name=provider_display_name,
            provider_protocol=provider_protocol,
            model=run.model,
            status=run.status,
            created_at=run.created_at,
            updated_at=run.updated_at,
            error=run.error,
        )


class RunDetail(BaseModel):
    run: RunRecord
    provider_display_name: str | None = None
    provider_protocol: ModelApiProtocol | None = None
    events: list[RunEvent] = Field(default_factory=list)
    pending_hard_rules: list[HardRuleAssessment] = Field(default_factory=list)
    human_rule_decisions: list[HumanRuleDecision] = Field(default_factory=list)
    human_review_summary: HumanReviewSummary = Field(default_factory=HumanReviewSummary)
    human_panel_decision: HumanPanelDecision | None = None


class ReportView(BaseModel):
    run: RunRecord
    provider_display_name: str | None = None
    provider_protocol: ModelApiProtocol | None = None
    document: DocumentInfo | None = None
    rubric: RubricProfile
    review: MetaReview
    audit: AuditReport
    evidence: list[EvidenceItem] = Field(default_factory=list)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    report_markdown: Path
    report_json: Path
    evaluation: EvaluationReport | None = None
    human_review_summary: HumanReviewSummary = Field(default_factory=HumanReviewSummary)
    pending_hard_rules: list[HardRuleAssessment] = Field(default_factory=list)
    human_panel_decision: HumanPanelDecision | None = None
    presentation_profile: ReportPresentationProfile = ReportPresentationProfile.LEGACY
    submission_metadata: SubmissionMetadata | None = None
