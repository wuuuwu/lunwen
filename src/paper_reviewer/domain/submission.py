from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SUBMISSION_METADATA_SCHEMA_VERSION: Literal["1.0"] = "1.0"
SUBMISSION_METADATA_FIELDS = ("student_name", "student_id", "major", "paper_title")


class SubmissionMetadataSource(StrEnum):
    COVER_LABEL = "cover_label"
    MODEL_EVIDENCE = "model_evidence"
    PDF_METADATA = "pdf_metadata"
    FILE_NAME = "file_name"
    HUMAN_CORRECTION = "human_correction"
    PLACEHOLDER = "placeholder"


class SubmissionFieldEvidence(BaseModel):
    source: SubmissionMetadataSource
    confidence: float = Field(ge=0, le=1)
    page: int | None = Field(default=None, ge=1)
    block_id: str | None = None
    evidence: str | None = None


class SubmissionMetadata(BaseModel):
    """Versioned, display-safe student and paper metadata.

    Values stay directly accessible because they are used frequently by report and
    filename projections. Provenance is deliberately kept in a parallel map so it
    can evolve without changing those projections.
    """

    schema_version: Literal["1.0"] = SUBMISSION_METADATA_SCHEMA_VERSION
    student_name: str
    student_id: str
    major: str
    paper_title: str
    field_evidence: dict[str, SubmissionFieldEvidence]
    warnings: list[str] = Field(default_factory=list)
    needs_review: bool = False

    @model_validator(mode="after")
    def validate_complete_provenance(self) -> SubmissionMetadata:
        expected = set(SUBMISSION_METADATA_FIELDS)
        actual = set(self.field_evidence)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise ValueError(
                "field_evidence must cover all metadata fields; "
                f"missing={missing}, unknown={unknown}"
            )
        values = (self.student_name, self.student_id, self.major, self.paper_title)
        if any(not value.strip() for value in values):
            raise ValueError("submission metadata values must not be blank")
        self.needs_review = bool(self.warnings) or any(
            detail.source is SubmissionMetadataSource.PLACEHOLDER
            or detail.confidence < 0.75
            for detail in self.field_evidence.values()
        )
        return self
