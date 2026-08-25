from __future__ import annotations

from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paper_reviewer.domain.review import PolicyContext


class ScoreAnchor(BaseModel):
    label: str
    minimum: float
    maximum: float
    description: str

    @model_validator(mode="after")
    def check_bounds(self) -> ScoreAnchor:
        if self.minimum > self.maximum:
            raise ValueError("score anchor minimum cannot exceed maximum")
        return self


class EvidencePolicy(BaseModel):
    paper_evidence_required: bool = True
    external_evidence_required: bool = False
    minimum_references: int = Field(default=1, ge=0)


class RubricDimension(BaseModel):
    dimension_id: str
    title: str
    description: str
    weight: float = Field(ge=0, le=100)
    minimum_score: float = 0
    maximum_score: float = Field(gt=0)
    checks: list[str] = Field(min_length=1)
    anchors: list[ScoreAnchor] = Field(default_factory=list)
    evidence_policy: EvidencePolicy = Field(default_factory=EvidencePolicy)
    reviewer_tags: list[str] = Field(default_factory=list)
    group_id: str | None = None

    @model_validator(mode="after")
    def check_dimension(self) -> RubricDimension:
        if self.minimum_score >= self.maximum_score:
            raise ValueError("dimension minimum_score must be below maximum_score")
        ordered = sorted(self.anchors, key=lambda item: item.minimum)
        for left, right in pairwise(ordered):
            if left.maximum >= right.minimum:
                raise ValueError(f"overlapping score anchors in {self.dimension_id}")
        for anchor in ordered:
            if anchor.minimum < self.minimum_score or anchor.maximum > self.maximum_score:
                raise ValueError(f"score anchor outside dimension range in {self.dimension_id}")
        return self


class HardRule(BaseModel):
    rule_id: str
    title: str | None = None
    description: str
    outcome: str
    evidence_required: bool = True
    requires_human_confirmation: bool = False
    ai_allowed_statuses: list[str] = Field(default_factory=list)


class AggregationPolicy(BaseModel):
    method: str = "weighted_mean"
    passing_score: float | None = None
    maximum_total: float = 100


class RubricGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    title: str
    description: str
    weight: float = Field(gt=0, le=100)
    dimensions: list[str] = Field(min_length=1)


class RatingScale(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: int = Field(strict=True)
    maximum: int = Field(strict=True)
    integer_only: bool = True
    anchors: list[ScoreAnchor] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def check_discrete_scale(self) -> RatingScale:
        if self.minimum != 0 or self.maximum != 4 or not self.integer_only:
            raise ValueError("schema v2 rating scale must be discrete integer 0-4")
        values = [(item.minimum, item.maximum) for item in self.anchors]
        if values != [(float(value), float(value)) for value in range(5)]:
            raise ValueError("schema v2 rating anchors must define exact levels 0, 1, 2, 3, 4")
        return self


class PanelStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_reviewers: int = Field(strict=True)
    initial_unqualified_threshold: int = Field(strict=True)
    supplemental_reviewers: int = Field(strict=True)
    supplemental_unqualified_threshold: int = Field(strict=True)
    supplemental_trigger: str
    unable_to_assess: str

    @model_validator(mode="after")
    def check_three_plus_two(self) -> PanelStrategy:
        if (
            self.initial_reviewers != 3
            or self.initial_unqualified_threshold != 2
            or self.supplemental_reviewers != 2
            or self.supplemental_unqualified_threshold != 1
        ):
            raise ValueError("schema v2 panel strategy must implement the deterministic 3+2 rule")
        return self


class RubricProfile(BaseModel):
    """Versioned rubric with strict policy rules for schema v2."""

    schema_version: str = "1"
    rubric_id: str
    version: str
    title: str
    applicable_levels: list[str] = Field(default_factory=list)
    applicable_paper_types: list[str] = Field(default_factory=list)
    dimensions: list[RubricDimension] = Field(default_factory=list)
    hard_rules: list[HardRule] = Field(default_factory=list)
    aggregation: AggregationPolicy | None = None
    scoring_enabled: bool = False

    # v2-only fields have defaults so old v1 task snapshots remain readable.
    policy_context: PolicyContext | None = None
    groups: list[RubricGroup] = Field(default_factory=list)
    rating_scale: RatingScale | None = None
    panel_strategy: PanelStrategy | None = None
    evaluation_mode: Literal["dual_advisory"] | None = None
    experimental: bool = False
    validation_notice: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_or_unknown_v2_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        version = str(value.get("schema_version", "1"))
        if version not in {"1", "2"}:
            raise ValueError(f"unsupported rubric schema_version: {version}")
        if version == "2":
            _check_v2_unknown_fields(value)
        return value

    @model_validator(mode="after")
    def check_profile(self) -> RubricProfile:
        ids = [dimension.dimension_id for dimension in self.dimensions]
        if len(ids) != len(set(ids)):
            raise ValueError("rubric dimension ids must be unique")
        rule_ids = [rule.rule_id for rule in self.hard_rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rubric hard rule ids must be unique")
        if self.scoring_enabled:
            if not self.dimensions or self.aggregation is None:
                raise ValueError("scored rubrics require dimensions and aggregation")
            total_weight = sum(dimension.weight for dimension in self.dimensions)
            if abs(total_weight - 100) > 0.01:
                raise ValueError(f"scored rubric weights must total 100, got {total_weight}")
            if any(not dimension.anchors for dimension in self.dimensions):
                raise ValueError("each scored dimension requires score anchors")
        if self.schema_version == "2":
            self._check_v2_profile()
        return self

    def _check_v2_profile(self) -> None:
        if self.policy_context is None:
            raise ValueError("schema v2 requires policy_context")
        if self.evaluation_mode != "dual_advisory":
            raise ValueError("schema v2 requires evaluation_mode=dual_advisory")
        if not self.scoring_enabled:
            raise ValueError("schema v2 dual-advisory rubric must enable diagnostic scoring")
        if self.aggregation is None or self.aggregation.method != "weighted_rating":
            raise ValueError("schema v2 requires aggregation.method=weighted_rating")
        if self.aggregation.passing_score is not None:
            raise ValueError("schema v2 passing_score must be null")
        if abs(self.aggregation.maximum_total - 100) > 0.001:
            raise ValueError("schema v2 maximum_total must be 100")
        if self.rating_scale is None:
            raise ValueError("schema v2 requires a discrete rating_scale")
        if self.panel_strategy is None:
            raise ValueError("schema v2 requires a 3+2 panel_strategy")
        if not self.experimental or not self.version.startswith("0."):
            raise ValueError("uncalibrated schema v2 rubrics must be marked 0.x experimental")
        if not self.validation_notice or "效度" not in self.validation_notice:
            raise ValueError("schema v2 requires an educational-validity validation_notice")
        if not self.groups:
            raise ValueError("schema v2 requires first-level indicator groups")
        if not self.hard_rules:
            raise ValueError("schema v2 requires structured hard rules")
        if any(not rule.requires_human_confirmation for rule in self.hard_rules):
            raise ValueError("schema v2 hard rules must require human confirmation")
        allowed_ai_statuses = {"not_detected", "suspected", "not_assessable"}
        if any(set(rule.ai_allowed_statuses) != allowed_ai_statuses for rule in self.hard_rules):
            raise ValueError(
                "schema v2 hard rules must restrict AI to not_detected/suspected/not_assessable"
            )

        dimension_by_id = {item.dimension_id: item for item in self.dimensions}
        grouped_ids: list[str] = []
        for group in self.groups:
            grouped_ids.extend(group.dimensions)
            unknown = set(group.dimensions) - set(dimension_by_id)
            if unknown:
                raise ValueError(
                    f"rubric group {group.group_id} references unknown dimensions: "
                    f"{sorted(unknown)}"
                )
            group_weight = sum(dimension_by_id[item].weight for item in group.dimensions)
            if abs(group_weight - group.weight) > 0.01:
                raise ValueError(
                    f"rubric group {group.group_id} weight {group.weight} does not match "
                    f"its dimensions {group_weight}"
                )
            for dimension_id in group.dimensions:
                if dimension_by_id[dimension_id].group_id != group.group_id:
                    raise ValueError(
                        f"dimension {dimension_id} group_id does not match group {group.group_id}"
                    )
        if len(grouped_ids) != len(set(grouped_ids)):
            raise ValueError("schema v2 dimensions may belong to only one group")
        if set(grouped_ids) != set(dimension_by_id):
            missing = set(dimension_by_id) - set(grouped_ids)
            raise ValueError(f"schema v2 groups do not cover every dimension: {sorted(missing)}")
        if abs(sum(group.weight for group in self.groups) - 100) > 0.01:
            raise ValueError("schema v2 group weights must total 100")

        expected_anchors = [(float(value), float(value)) for value in range(5)]
        for dimension in self.dimensions:
            if dimension.minimum_score != 0 or dimension.maximum_score != 4:
                raise ValueError(
                    f"schema v2 dimension {dimension.dimension_id} must use the 0-4 range"
                )
            values = [
                (anchor.minimum, anchor.maximum)
                for anchor in sorted(dimension.anchors, key=lambda item: item.minimum)
            ]
            if values != expected_anchors:
                raise ValueError(
                    f"schema v2 dimension {dimension.dimension_id} requires exact 0-4 anchors"
                )


def _forbid_unknown(payload: object, *, path: str, allowed: set[str]) -> None:
    if not isinstance(payload, dict):
        return
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unknown schema v2 field(s) at {path}: {sorted(unknown)}")


def _check_v2_unknown_fields(payload: dict[str, Any]) -> None:
    _forbid_unknown(
        payload,
        path="rubric",
        allowed={
            "schema_version",
            "rubric_id",
            "version",
            "title",
            "applicable_levels",
            "applicable_paper_types",
            "dimensions",
            "hard_rules",
            "aggregation",
            "scoring_enabled",
            "policy_context",
            "groups",
            "rating_scale",
            "panel_strategy",
            "evaluation_mode",
            "experimental",
            "validation_notice",
        },
    )
    nested_specs: list[tuple[str, set[str]]] = [
        (
            "dimensions",
            {
                "dimension_id",
                "title",
                "description",
                "weight",
                "minimum_score",
                "maximum_score",
                "checks",
                "anchors",
                "evidence_policy",
                "reviewer_tags",
                "group_id",
            },
        ),
        (
            "hard_rules",
            {
                "rule_id",
                "title",
                "description",
                "outcome",
                "evidence_required",
                "requires_human_confirmation",
                "ai_allowed_statuses",
            },
        ),
        ("groups", {"group_id", "title", "description", "weight", "dimensions"}),
    ]
    for key, allowed in nested_specs:
        values = payload.get(key, [])
        if isinstance(values, list):
            for index, item in enumerate(values):
                _forbid_unknown(item, path=f"{key}[{index}]", allowed=allowed)
    _forbid_unknown(
        payload.get("policy_context"),
        path="policy_context",
        allowed={"source", "document_number", "effective_date", "source_sha256"},
    )
    _forbid_unknown(
        payload.get("rating_scale"),
        path="rating_scale",
        allowed={"minimum", "maximum", "integer_only", "anchors"},
    )
    _forbid_unknown(
        payload.get("panel_strategy"),
        path="panel_strategy",
        allowed={
            "initial_reviewers",
            "initial_unqualified_threshold",
            "supplemental_reviewers",
            "supplemental_unqualified_threshold",
            "supplemental_trigger",
            "unable_to_assess",
        },
    )
    _forbid_unknown(
        payload.get("aggregation"),
        path="aggregation",
        allowed={"method", "passing_score", "maximum_total"},
    )
    for index, dimension in enumerate(payload.get("dimensions", [])):
        if not isinstance(dimension, dict):
            continue
        _forbid_unknown(
            dimension.get("evidence_policy"),
            path=f"dimensions[{index}].evidence_policy",
            allowed={
                "paper_evidence_required",
                "external_evidence_required",
                "minimum_references",
            },
        )
        for anchor_index, anchor in enumerate(dimension.get("anchors", [])):
            _forbid_unknown(
                anchor,
                path=f"dimensions[{index}].anchors[{anchor_index}]",
                allowed={"label", "minimum", "maximum", "description"},
            )
    rating_scale = payload.get("rating_scale")
    if isinstance(rating_scale, dict):
        for index, anchor in enumerate(rating_scale.get("anchors", [])):
            _forbid_unknown(
                anchor,
                path=f"rating_scale.anchors[{index}]",
                allowed={"label", "minimum", "maximum", "description"},
            )
