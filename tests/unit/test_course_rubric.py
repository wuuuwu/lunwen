from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_reviewer.application.review_planner import build_review_plan
from paper_reviewer.config import load_review_profile, load_rubric
from paper_reviewer.domain.review import ReviewerResult, ScoreProposal
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.validation.scoring import aggregate_scores

COURSE_RUBRIC = Path("configs/rubrics/course_paper_v1.yaml")
COURSE_REVIEWERS = Path("configs/review_profiles/course_paper_reviewers_v1.yaml")


def test_bundled_course_rubric_has_the_general_six_dimension_contract() -> None:
    rubric = load_rubric(COURSE_RUBRIC)

    assert rubric.schema_version == "1"
    assert rubric.evaluation_mode == "course_assessment"
    assert rubric.version == "0.1.0-experimental"
    assert rubric.experimental is True
    assert rubric.validation_notice is not None
    assert "课程大纲" in rubric.validation_notice
    assert rubric.hard_rules == []
    assert [dimension.dimension_id for dimension in rubric.dimensions] == [
        "task_completion",
        "course_knowledge_application",
        "argument_evidence",
        "structure_logic",
        "writing_expression",
        "citation_format",
    ]
    assert [dimension.weight for dimension in rubric.dimensions] == [25, 25, 20, 15, 10, 5]
    assert rubric.aggregation is not None
    assert rubric.aggregation.method == "weighted_mean"
    assert rubric.aggregation.passing_score == 60
    assert rubric.aggregation.maximum_total == 100


def test_bundled_course_dimensions_use_shared_zero_to_one_hundred_anchors() -> None:
    rubric = load_rubric(COURSE_RUBRIC)
    expected_ranges = [(0, 39), (40, 59), (60, 74), (75, 89), (90, 100)]

    for dimension in rubric.dimensions:
        assert (dimension.minimum_score, dimension.maximum_score) == (0, 100)
        assert [(anchor.minimum, anchor.maximum) for anchor in dimension.anchors] == expected_ranges
        assert dimension.evidence_policy.paper_evidence_required is True
        assert dimension.evidence_policy.external_evidence_required is False


def test_course_reviewer_profile_assigns_exactly_two_dimensions_per_reviewer() -> None:
    rubric = load_rubric(COURSE_RUBRIC)
    profile = load_review_profile(COURSE_REVIEWERS)
    plan = build_review_plan(rubric, profile)

    expected = {
        "course-requirements-reviewer": {
            "task_completion",
            "course_knowledge_application",
        },
        "argumentation-reviewer": {"argument_evidence", "structure_logic"},
        "writing-norms-reviewer": {"writing_expression", "citation_format"},
    }
    assert len(plan.assignments) == 3
    assert {
        assignment.reviewer.reviewer_id: set(assignment.dimension_ids)
        for assignment in plan.assignments
    } == expected
    assert [reviewer.dimension_tags for reviewer in profile.reviewers] == [
        ["course_requirements"],
        ["argumentation"],
        ["writing_norms"],
    ]


def test_bundled_course_rubric_uses_sixty_as_the_inclusive_pass_threshold() -> None:
    rubric = load_rubric(COURSE_RUBRIC)
    result = ReviewerResult(
        reviewer_id="combined-test-result",
        summary="",
        findings=[],
        dimension_scores={
            dimension.dimension_id: ScoreProposal(score=60, explanation="test")
            for dimension in rubric.dimensions
        },
    )

    aggregated = aggregate_scores(rubric, [result])

    assert aggregated.total_score == 60
    assert aggregated.verdict == "pass"


def test_course_mode_validates_aggregation_without_fixing_course_specific_content() -> None:
    payload = load_rubric(COURSE_RUBRIC).model_dump(mode="json")
    payload["dimensions"] = payload["dimensions"][:1]
    payload["dimensions"][0]["weight"] = 100
    payload["aggregation"]["passing_score"] = 55
    customized = RubricProfile.model_validate(payload)
    assert customized.aggregation is not None
    assert customized.aggregation.passing_score == 55

    payload["aggregation"]["method"] = "weighted_rating"
    with pytest.raises(
        ValidationError,
        match=r"course assessment requires aggregation\.method=weighted_mean",
    ):
        RubricProfile.model_validate(payload)

    payload["aggregation"]["method"] = "weighted_mean"
    payload["aggregation"]["maximum_total"] = 10
    with pytest.raises(ValidationError, match="course assessment maximum_total must be 100"):
        RubricProfile.model_validate(payload)


def test_legacy_schema_v1_remains_compatible_without_a_course_mode() -> None:
    legacy = RubricProfile.model_validate(
        {
            "schema_version": "1",
            "rubric_id": "legacy",
            "version": "1.0.0",
            "title": "Legacy",
            "scoring_enabled": False,
        }
    )
    assert legacy.evaluation_mode is None
    assert legacy.dimensions == []
