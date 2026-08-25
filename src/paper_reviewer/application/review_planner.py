from __future__ import annotations

from pydantic import BaseModel

from paper_reviewer.config import ReviewerProfile, ReviewProfile
from paper_reviewer.domain.rubric import RubricProfile


class ReviewAssignment(BaseModel):
    reviewer: ReviewerProfile
    dimension_ids: list[str]


class ReviewPlan(BaseModel):
    assignments: list[ReviewAssignment]


def build_review_plan(rubric: RubricProfile, profile: ReviewProfile) -> ReviewPlan:
    dimension_ids = {dimension.dimension_id for dimension in rubric.dimensions}
    dimension_tags = {
        dimension.dimension_id: set(dimension.reviewer_tags) for dimension in rubric.dimensions
    }
    assignments: list[ReviewAssignment] = []
    covered: set[str] = set()
    for reviewer in profile.reviewers:
        explicit = set(reviewer.dimension_ids)
        unknown = explicit - dimension_ids
        if unknown:
            raise ValueError(
                f"reviewer {reviewer.reviewer_id} references unknown dimensions: {sorted(unknown)}"
            )
        by_tag = {
            dimension_id
            for dimension_id, tags in dimension_tags.items()
            if tags.intersection(reviewer.dimension_tags)
        }
        assigned = sorted(explicit | by_tag)
        covered.update(assigned)
        assignments.append(ReviewAssignment(reviewer=reviewer, dimension_ids=assigned))
    uncovered = dimension_ids - covered
    if uncovered:
        raise ValueError(f"rubric dimensions are not assigned to a reviewer: {sorted(uncovered)}")
    return ReviewPlan(assignments=assignments)
