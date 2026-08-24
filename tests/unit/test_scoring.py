from __future__ import annotations

from pathlib import Path

import pytest

from paper_reviewer.config import load_rubric
from paper_reviewer.domain.review import CriterionAssessment, ReviewerResult, ScoreProposal
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.validation.scoring import aggregate_scores, calculate_diagnostic_score


def _rubric() -> RubricProfile:
    return RubricProfile.model_validate(
        {
            "rubric_id": "test",
            "version": "1",
            "title": "Test",
            "scoring_enabled": True,
            "aggregation": {
                "method": "weighted_mean",
                "maximum_total": 100,
                "passing_score": 60,
            },
            "dimensions": [
                {
                    "dimension_id": "methods",
                    "title": "Methods",
                    "description": "Methods",
                    "weight": 100,
                    "minimum_score": 0,
                    "maximum_score": 10,
                    "checks": ["Check methods"],
                    "anchors": [
                        {
                            "label": "range",
                            "minimum": 0,
                            "maximum": 10,
                            "description": "Full range",
                        }
                    ],
                }
            ],
        }
    )


def test_scores_are_aggregated_deterministically() -> None:
    results = [
        ReviewerResult(
            reviewer_id="one",
            summary="",
            findings=[],
            dimension_scores={"methods": ScoreProposal(score=6, explanation="")},
        ),
        ReviewerResult(
            reviewer_id="two",
            summary="",
            findings=[],
            dimension_scores={"methods": ScoreProposal(score=8, explanation="")},
        ),
    ]
    score = aggregate_scores(_rubric(), results)
    assert score.total_score == 70
    assert score.verdict == "pass"
    assert score.dimension_scores == {"methods": 7}


def test_missing_dimension_score_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing score"):
        aggregate_scores(_rubric(), [])


def test_zhejiang_diagnostic_score_uses_discrete_weighted_ratings() -> None:
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    ratings = [4, 3, 2, 1, 0, 4, 3, 2, 1]
    assessments = [
        CriterionAssessment(
            criterion_id=dimension.dimension_id,
            reviewer_id="specialist",
            rating=rating,
            weight=dimension.weight,
            rationale="evidence-grounded assessment",
            confidence=0.5,
        )
        for dimension, rating in zip(rubric.dimensions, ratings, strict=True)
    ]
    result = calculate_diagnostic_score(rubric, assessments)
    expected = sum(
        rating / 4 * dimension.weight
        for dimension, rating in zip(rubric.dimensions, ratings, strict=True)
    )
    assert result.total_score == round(expected, 2)
    assert result.group_scores == {
        "topic_significance": 17.5,
        "logical_construction": 7.5,
        "professional_level": 27.5,
        "academic_norms": 7.5,
    }


def test_zhejiang_diagnostic_score_rejects_missing_or_unknown_criterion() -> None:
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    assessment = CriterionAssessment(
        criterion_id="unknown",
        reviewer_id="specialist",
        rating=2,
        weight=10,
        rationale="test",
        confidence=0.5,
    )
    with pytest.raises(ValueError, match="unknown rubric criterion"):
        calculate_diagnostic_score(rubric, [assessment])
