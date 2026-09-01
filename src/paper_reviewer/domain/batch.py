from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from paper_reviewer.config import ReviewProfile
from paper_reviewer.domain.provider import ProviderSnapshot
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.submission import SubmissionMetadata


class BatchStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"


class BatchItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SOURCE_CHANGED = "source_changed"


class BatchReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_dir: Path
    output_dir: Path
    provider: str
    model: str
    rubric: Path
    profile: Path
    cloud_processing_authorized: bool = False
    contains_classified_material: bool = False
    pii_output_authorized: bool = False
    external_search: bool = False

    @field_validator("source_dir", "output_dir", "rubric", "profile")
    @classmethod
    def make_paths_absolute(cls, value: Path) -> Path:
        return value.expanduser().resolve(strict=False)

    @field_validator("provider", "model")
    @classmethod
    def require_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized


class BatchSourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)
    duplicate_sha256: bool = False


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    source: BatchSourceSnapshot
    status: BatchItemStatus = BatchItemStatus.QUEUED
    run_id: str | None = None
    metadata: SubmissionMetadata | None = None
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    total_score: float | None = None
    grade: str | None = None
    conclusion: str | None = None
    report_path: Path | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BatchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"] = "2"
    batch_id: str
    status: BatchStatus = BatchStatus.CREATED
    request: BatchReviewRequest
    rubric_snapshot: RubricProfile
    profile_snapshot: ReviewProfile
    provider_snapshot: ProviderSnapshot
    items: list[BatchItem] = Field(min_length=1, max_length=100)
    current_item_id: str | None = None
    retry_item_ids: list[str] | None = None
    summary_path: Path | None = None
    workbook_path: Path | None = None
    workbook_export_error: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_items(self) -> BatchRecord:
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("batch item ids must be unique")
        if self.current_item_id is not None and self.current_item_id not in set(item_ids):
            raise ValueError("current_item_id must reference a batch item")
        if self.retry_item_ids is not None:
            if len(self.retry_item_ids) != len(set(self.retry_item_ids)):
                raise ValueError("retry_item_ids must be unique")
            if not set(self.retry_item_ids).issubset(item_ids):
                raise ValueError("retry_item_ids must reference batch items")
        if self.provider_snapshot.provider_ref != self.request.provider:
            raise ValueError("provider snapshot must match the batch request")
        if self.provider_snapshot.model != self.request.model:
            raise ValueError("provider snapshot model must match the batch request")
        source_root = self.request.source_dir
        if any(item.source.path.parent != source_root for item in self.items):
            raise ValueError("batch item sources must be top-level files in source_dir")
        output_root = self.request.output_dir.resolve(strict=False)
        for item in self.items:
            if item.report_path is None:
                continue
            report_path = item.report_path.expanduser().resolve(strict=False)
            if report_path == output_root or not report_path.is_relative_to(output_root):
                raise ValueError("batch report paths must remain inside output_dir")
        if self.summary_path is not None:
            summary = self.summary_path.resolve(strict=False)
            if summary.parent != output_root or summary.suffix.casefold() != ".csv":
                raise ValueError("batch summary must be a CSV inside output_dir")
        if self.workbook_path is not None:
            workbook = self.workbook_path.resolve(strict=False)
            if workbook.parent != output_root or workbook.suffix.casefold() != ".xlsx":
                raise ValueError("batch workbook must be an XLSX inside output_dir")
        return self


class BatchEvent(BaseModel):
    batch_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: str
    status: BatchStatus | None = None
    item_id: str | None = None
    item_status: BatchItemStatus | None = None
    message: str
    payload: dict[str, object] = Field(default_factory=dict)
