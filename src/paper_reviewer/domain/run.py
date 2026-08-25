from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    CREATED = "created"
    INGESTING = "ingesting"
    INGESTED = "ingested"
    BUILDING_EVIDENCE = "building_evidence"
    EVIDENCE_READY = "evidence_ready"
    REVIEWING = "reviewing"
    # v2 scoring and panel states.  Keep the legacy values above unchanged so
    # snapshots written by older versions remain readable.
    SCORING = "scoring"
    AUDITING = "auditing"
    AWAITING_HARD_RULE_CONFIRMATION = "awaiting_hard_rule_confirmation"
    PANEL_REVIEWING = "panel_reviewing"
    SUPPLEMENTAL_REVIEWING = "supplemental_reviewing"
    AWAITING_PANEL_REVIEW = "awaiting_panel_review"
    SYNTHESIZING = "synthesizing"
    META_REVIEWING = "meta_reviewing"
    VALIDATING = "validating"
    REPORTED_PENDING_HUMAN_REVIEW = "reported_pending_human_review"
    REPORTED = "reported"
    RETRYABLE_FAILURE = "retryable_failure"
    FATAL_FAILURE = "fatal_failure"
    CANCELLED = "cancelled"


class RunRecord(BaseModel):
    run_id: str
    status: RunStatus = RunStatus.CREATED
    input_path: str
    input_hash: str
    config_hash: str
    rubric_id: str
    provider: str
    model: str
    completed_stages: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
