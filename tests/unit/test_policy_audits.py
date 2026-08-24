from __future__ import annotations

import pytest

from paper_reviewer.domain.document import DocumentBlock
from paper_reviewer.domain.review import (
    CriterionAssessment,
    ExpertOpinion,
    ReviewerResult,
    ReviewFinding,
)
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.validation.audits import (
    audit_criterion_assessments,
    audit_expert_opinions,
)


def _paper_reference(block: DocumentBlock) -> dict[str, object]:
    return {
        "evidence_id": f"paper:{block.block_id}",
        "kind": "paper",
        "block_id": block.block_id,
        "page": block.page,
        "quote": block.text,
    }


def _rubric() -> RubricProfile:
    return RubricProfile.model_validate(
        {
            "rubric_id": "diagnostic",
            "version": "0.1-experimental",
            "title": "Diagnostic",
            "scoring_enabled": True,
            "aggregation": {"method": "weighted_rating", "passing_score": None},
            "dimensions": [
                {
                    "dimension_id": "purpose",
                    "title": "Purpose",
                    "description": "Purpose",
                    "weight": 100,
                    "minimum_score": 0,
                    "maximum_score": 4,
                    "checks": ["purpose"],
                    "anchors": [
                        {
                            "label": "all",
                            "minimum": 0,
                            "maximum": 4,
                            "description": "Discrete scale",
                        }
                    ],
                }
            ],
        }
    )


def test_diagnostic_audit_checks_weight_assignment_and_evidence_location() -> None:
    block = DocumentBlock.create(document_id="doc", page=2, text="Purpose evidence.")
    assessment = CriterionAssessment.model_validate(
        {
            "criterion_id": "purpose",
            "reviewer_id": "wrong-reviewer",
            "rating": 2,
            "weight": 50,
            "rationale": "Partly adequate.",
            "confidence": 0.7,
            "paper_evidence": [
                {**_paper_reference(block), "page": 1, "quote": "Invented quote"}
            ],
        }
    )
    audit = audit_criterion_assessments(
        assessments=[assessment],
        rubric=_rubric(),
        blocks=[block],
        evidence=[],
        reviewer_dimensions={"purpose-reviewer": {"purpose"}},
    )
    assert any("does not match rubric weight" in item for item in audit.errors)
    assert any("is not assigned to reviewer" in item for item in audit.errors)
    assert any("does not match block page" in item for item in audit.errors)
    assert any("quote does not match" in item for item in audit.errors)


def test_expert_audit_rejects_unknown_or_non_major_findings() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="Evidence.")
    minor = ReviewFinding.model_validate(
        {
            "finding_id": "minor-1",
            "reviewer_id": "reviewer",
            "dimension_id": "purpose",
            "severity": "minor",
            "confidence": 0.8,
            "claim": "A minor issue.",
            "rationale": "Minor.",
            "paper_evidence": [_paper_reference(block)],
            "recommendation": "Improve it.",
        }
    )
    opinion = ExpertOpinion(
        expert_id="expert",
        round="initial",
        verdict="unqualified",
        rationale="Not qualified.",
        finding_ids=["minor-1", "invented"],
    )
    audit = audit_expert_opinions(
        opinions=[opinion], findings=[minor], blocks=[block], evidence=[]
    )
    assert any("unknown finding invented" in item for item in audit.errors)
    assert any("does not cite a major finding" in item for item in audit.errors)


def test_legacy_reviewer_result_remains_readable() -> None:
    result = ReviewerResult.model_validate(
        {"reviewer_id": "legacy", "summary": "Old", "findings": []}
    )
    assert result.dimension_scores == {}


@pytest.mark.parametrize("invalid_rating", [2.0, "2"])
def test_criterion_rating_is_a_strict_integer(invalid_rating: object) -> None:
    with pytest.raises(ValueError):
        CriterionAssessment.model_validate(
            {
                "criterion_id": "purpose",
                "reviewer_id": "purpose-reviewer",
                "rating": invalid_rating,
                "weight": 100,
                "rationale": "Assessment.",
                "confidence": 0.7,
            }
        )
