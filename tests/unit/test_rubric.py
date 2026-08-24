from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_reviewer.config import load_rubric
from paper_reviewer.domain.rubric import RubricProfile


def test_unscored_rubric_can_have_no_dimensions() -> None:
    rubric = RubricProfile(
        rubric_id="draft",
        version="1",
        title="Draft",
        scoring_enabled=False,
    )
    assert rubric.dimensions == []


def test_scored_rubric_requires_weights_to_total_one_hundred() -> None:
    with pytest.raises(ValidationError, match="weights must total 100"):
        RubricProfile.model_validate(
            {
                "rubric_id": "bad",
                "version": "1",
                "title": "Bad",
                "scoring_enabled": True,
                "aggregation": {"method": "weighted_mean", "maximum_total": 100},
                "dimensions": [
                    {
                        "dimension_id": "methods",
                        "title": "Methods",
                        "description": "Method quality",
                        "weight": 90,
                        "minimum_score": 0,
                        "maximum_score": 10,
                        "checks": ["Is the method appropriate?"],
                        "anchors": [
                            {
                                "label": "all",
                                "minimum": 0,
                                "maximum": 10,
                                "description": "Complete range",
                            }
                        ],
                    }
                ],
            }
        )


def test_overlapping_anchors_are_rejected() -> None:
    with pytest.raises(ValidationError, match="overlapping score anchors"):
        RubricProfile.model_validate(
            {
                "rubric_id": "bad",
                "version": "1",
                "title": "Bad",
                "scoring_enabled": False,
                "dimensions": [
                    {
                        "dimension_id": "methods",
                        "title": "Methods",
                        "description": "Method quality",
                        "weight": 100,
                        "minimum_score": 0,
                        "maximum_score": 10,
                        "checks": ["Check"],
                        "anchors": [
                            {"label": "low", "minimum": 0, "maximum": 5, "description": "Low"},
                            {
                                "label": "high",
                                "minimum": 5,
                                "maximum": 10,
                                "description": "High",
                            },
                        ],
                    }
                ],
            }
        )


def test_bundled_zhejiang_v2_rubric_is_strict_and_complete() -> None:
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    assert rubric.schema_version == "2"
    assert rubric.evaluation_mode == "dual_advisory"
    assert rubric.policy_context is not None
    assert "2023" in rubric.policy_context.document_number
    assert len(rubric.dimensions) == 9
    assert sum(item.weight for item in rubric.dimensions) == 100
    assert rubric.aggregation is not None
    assert rubric.aggregation.method == "weighted_rating"
    assert rubric.aggregation.passing_score is None
    assert rubric.panel_strategy is not None
    assert (
        rubric.panel_strategy.initial_reviewers,
        rubric.panel_strategy.supplemental_reviewers,
    ) == (
        3,
        2,
    )


def test_schema_v2_rejects_unknown_fields_and_versions() -> None:
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml")).model_dump(
        mode="json"
    )
    rubric["unknown_policy_switch"] = True
    with pytest.raises(ValidationError, match="unknown schema v2 field"):
        RubricProfile.model_validate(rubric)

    rubric.pop("unknown_policy_switch")
    rubric["schema_version"] = "3"
    with pytest.raises(ValidationError, match="unsupported rubric schema_version"):
        RubricProfile.model_validate(rubric)
