from __future__ import annotations

from statistics import mean

from pydantic import BaseModel, Field

from paper_reviewer.domain.review import CriterionAssessment, DiagnosticScore, ReviewerResult
from paper_reviewer.domain.rubric import RubricProfile


class AggregatedScore(BaseModel):
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    total_score: float | None = None
    verdict: str | None = None


def calculate_diagnostic_score(
    rubric: RubricProfile,
    assessments: list[CriterionAssessment],
) -> DiagnosticScore:
    """Calculate the v2 experimental diagnostic score without a pass/fail result."""

    if rubric.schema_version != "2" or rubric.aggregation is None:
        raise ValueError("diagnostic weighted ratings require a schema v2 rubric")
    if rubric.aggregation.method != "weighted_rating":
        raise ValueError("diagnostic weighted ratings require aggregation.method=weighted_rating")
    dimensions = {item.dimension_id: item for item in rubric.dimensions}
    by_id: dict[str, CriterionAssessment] = {}
    for assessment in assessments:
        if assessment.criterion_id not in dimensions:
            raise ValueError(f"unknown rubric criterion: {assessment.criterion_id}")
        if assessment.criterion_id in by_id:
            raise ValueError(f"duplicate criterion assessment: {assessment.criterion_id}")
        dimension = dimensions[assessment.criterion_id]
        if abs(assessment.weight - dimension.weight) > 0.001:
            raise ValueError(f"criterion {assessment.criterion_id} weight does not match rubric")
        by_id[assessment.criterion_id] = assessment
    missing = set(dimensions) - set(by_id)
    if missing:
        raise ValueError(f"missing criterion assessments: {sorted(missing)}")

    ordered = [by_id[item.dimension_id] for item in rubric.dimensions]
    group_scores: dict[str, float] = {}
    for group in rubric.groups:
        group_scores[group.group_id] = round(
            sum(by_id[item].weighted_contribution for item in group.dimensions), 2
        )
    total = round(sum(item.weighted_contribution for item in ordered), 2)
    return DiagnosticScore(
        assessments=ordered,
        group_scores=group_scores,
        total_score=total,
    )


def aggregate_scores(rubric: RubricProfile, results: list[ReviewerResult]) -> AggregatedScore:
    if not rubric.scoring_enabled:
        return AggregatedScore()
    if rubric.aggregation is None:
        raise ValueError("scored rubric is missing aggregation policy")
    scores: dict[str, list[float]] = {dimension.dimension_id: [] for dimension in rubric.dimensions}
    for result in results:
        for dimension_id, proposal in result.dimension_scores.items():
            if dimension_id in scores:
                scores[dimension_id].append(proposal.score)
    averages: dict[str, float] = {}
    weighted_total = 0.0
    for dimension in rubric.dimensions:
        values = scores[dimension.dimension_id]
        if not values:
            raise ValueError(f"missing score for rubric dimension: {dimension.dimension_id}")
        for score in values:
            if score < dimension.minimum_score or score > dimension.maximum_score:
                raise ValueError(f"score {score} is outside range for {dimension.dimension_id}")
        average = mean(values)
        averages[dimension.dimension_id] = round(average, 4)
        normalized = (average - dimension.minimum_score) / (
            dimension.maximum_score - dimension.minimum_score
        )
        weighted_total += normalized * dimension.weight
    total = round(weighted_total / 100 * rubric.aggregation.maximum_total, 2)
    verdict = None
    if rubric.aggregation.passing_score is not None:
        verdict = "pass" if total >= rubric.aggregation.passing_score else "fail"
    return AggregatedScore(
        dimension_scores=averages,
        total_score=total,
        verdict=verdict,
    )
