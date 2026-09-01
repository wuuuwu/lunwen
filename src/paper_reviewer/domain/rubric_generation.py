from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from paper_reviewer.config import ReviewProfile
from paper_reviewer.domain.rubric import RubricProfile


class SubjectAssessmentMode(StrEnum):
    NONE = "none"
    BASIC = "basic"
    SPECIALIST = "specialist"


ReviewerRole = Literal[
    "course_requirements",
    "subject_matter",
    "argumentation",
    "writing_norms",
]


class DimensionPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    weight: float = Field(gt=0, le=100)
    reviewer_role: ReviewerRole

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()


class CourseAssessmentBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_name: str = Field(min_length=1, max_length=200)
    course_level: str = Field(default="undergraduate", min_length=1, max_length=80)
    paper_type: str = Field(default="course_paper_pdf", min_length=1, max_length=80)
    assignment_requirements: str = Field(min_length=1, max_length=20_000)
    learning_outcomes: list[str] = Field(default_factory=list, max_length=30)
    subject_assessment_mode: SubjectAssessmentMode = SubjectAssessmentMode.BASIC
    subject_name: str = Field(default="", max_length=200)
    core_topics: list[str] = Field(default_factory=list, max_length=80)
    common_errors: list[str] = Field(default_factory=list, max_length=80)
    external_evidence_required: bool = False
    dimension_preferences: list[DimensionPreference] = Field(min_length=2, max_length=10)

    @field_validator(
        "course_name", "course_level", "paper_type", "assignment_requirements", "subject_name"
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("learning_outcomes", "core_topics", "common_errors")
    @classmethod
    def clean_lines(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def validate_design(self) -> CourseAssessmentBrief:
        if self.subject_assessment_mode is not SubjectAssessmentMode.NONE:
            if not self.subject_name:
                raise ValueError("启用课程内容评价时必须填写课程领域")
            if not self.learning_outcomes and not self.core_topics:
                raise ValueError("启用课程内容评价时必须提供学习目标或核心知识点")
        if self.subject_assessment_mode is SubjectAssessmentMode.NONE and any(
            item.reviewer_role == "subject_matter" for item in self.dimension_preferences
        ):
            raise ValueError("不评价课程内容时不能分配课程内容 Reviewer")
        total = sum(item.weight for item in self.dimension_preferences)
        if abs(total - 100) > 0.01:
            raise ValueError(f"评价维度权重必须合计 100，当前为 {total:g}")
        normalized_titles = [item.title.casefold() for item in self.dimension_preferences]
        if len(normalized_titles) != len(set(normalized_titles)):
            raise ValueError("评价维度名称不能重复")
        return self


class ScoringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_score: int = 0
    maximum_score: int = 100
    anchor_count: Literal[4, 5] = 5
    passing_score: float | None = 60
    maximum_total: Literal[100] = 100
    integer_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_range(self) -> ScoringSettings:
        if self.minimum_score >= self.maximum_score:
            raise ValueError("评分下限必须小于评分上限")
        available_values = self.maximum_score - self.minimum_score + 1
        if available_values < self.anchor_count:
            raise ValueError("评分范围不足以容纳所选评分等级")
        if self.passing_score is not None and not 0 <= self.passing_score <= 100:
            raise ValueError("及格线必须位于总分 0–100 之间")
        return self


class RubricGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: CourseAssessmentBrief
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    additional_instructions: str = Field(default="", max_length=8_000)

    @field_validator("additional_instructions")
    @classmethod
    def strip_instructions(cls, value: str) -> str:
        return value.strip()


class AnchorDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1_000)

    @field_validator("label", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class RubricDimensionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: str = Field(default="", max_length=80)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1_500)
    weight: float = Field(gt=0, le=100)
    reviewer_role: ReviewerRole
    checks: list[str] = Field(min_length=1, max_length=12)
    anchors: list[AnchorDraft] = Field(min_length=4, max_length=5)

    @field_validator("dimension_id", "title", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("checks")
    @classmethod
    def clean_checks(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("评价维度至少需要一个检查点")
        return cleaned


class RubricDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    dimensions: list[RubricDimensionDraft] = Field(min_length=2, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("assumptions")
    @classmethod
    def clean_assumptions(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class RubricGenerationResult(BaseModel):
    request: RubricGenerationRequest
    draft: RubricDraft
    rubric: RubricProfile
    profile: ReviewProfile
    warnings: list[str] = Field(default_factory=list)


class RubricPackageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    package_id: str
    rubric_id: str
    version: str
    title: str
    rubric_file: str = "rubric.yaml"
    profile_file: str = "reviewer_profile.yaml"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider_ref: str = ""
    model: str = ""
    parent_package_id: str | None = None
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SavedRubricPackage(BaseModel):
    root: Path
    rubric_path: Path
    profile_path: Path
    manifest_path: Path
    manifest: RubricPackageManifest
