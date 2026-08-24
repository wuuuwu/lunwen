from __future__ import annotations

from collections import deque

import pytest

from paper_reviewer.agents.meta_reviewer import run_meta_reviewer
from paper_reviewer.domain.evidence import EvidenceKind, EvidenceRef
from paper_reviewer.domain.review import ReviewerResult, ReviewFinding, Severity
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.ports.model import ModelRequest, ModelResponse, Usage
from paper_reviewer.validation.audits import AuditReport


class RecordingModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.popleft()


@pytest.mark.asyncio
async def test_meta_reviewer_uses_raised_output_budget_and_bounded_retry() -> None:
    model = RecordingModel(
        [
            ModelResponse(
                content='{"run_id":"run-1"',
                finish_reason="length",
                usage=Usage(output_tokens=8192),
            ),
            ModelResponse(
                content=(
                    '{"run_id":"run-1","overall_summary":"完成",'
                    '"selected_finding_ids":[],"disagreements":[],"human_checks":[]}'
                ),
                finish_reason="stop",
            ),
        ]
    )
    rubric = RubricProfile(
        rubric_id="unscored",
        version="1.0.0",
        title="Unscored rubric",
    )

    result = await run_meta_reviewer(
        run_id="run-1",
        model=model,
        rubric=rubric,
        results=[],
        audit=AuditReport(),
        max_repairs=1,
    )

    assert result.run_id == "run-1"
    assert [request.max_output_tokens for request in model.requests] == [8192, 16384]
    assert all(request.tools == [] for request in model.requests)


def _source_result() -> tuple[ReviewerResult, ReviewFinding]:
    finding = ReviewFinding(
        finding_id="methods-reviewer:missing-method",
        reviewer_id="methods-reviewer",
        dimension_id="methods",
        severity=Severity.MAJOR,
        confidence=0.9,
        claim="The method is underspecified.",
        rationale="Important implementation details are absent.",
        paper_evidence=[
            EvidenceRef(
                evidence_id="paper:block-1",
                kind=EvidenceKind.PAPER,
                block_id="block-1",
                page=2,
                quote="We apply the method.",
            )
        ],
        recommendation="Describe the complete procedure.",
    )
    return (
        ReviewerResult(
            reviewer_id="methods-reviewer",
            summary="Methods need clarification.",
            findings=[finding],
        ),
        finding,
    )


@pytest.mark.asyncio
async def test_meta_reviewer_copies_selected_source_finding_without_rewriting() -> None:
    source_result, source_finding = _source_result()
    model = RecordingModel(
        [
            ModelResponse(
                content=(
                    '{"run_id":"run-1","overall_summary":"需要补充方法细节",'
                    '"selected_finding_ids":["methods-reviewer:missing-method"],'
                    '"disagreements":[],"human_checks":[]}'
                ),
                finish_reason="stop",
            )
        ]
    )

    result = await run_meta_reviewer(
        run_id="run-1",
        model=model,
        rubric=RubricProfile(rubric_id="unscored", version="1", title="Unscored"),
        results=[source_result],
        audit=AuditReport(),
        max_repairs=1,
    )

    assert result.findings == [source_finding]
    assert result.findings[0] is not source_finding
    assert result.total_score is None
    assert result.verdict is None
    assert "selected_finding_ids" not in result.model_dump()


@pytest.mark.asyncio
async def test_meta_reviewer_repairs_invented_merged_finding_id() -> None:
    source_result, source_finding = _source_result()
    model = RecordingModel(
        [
            ModelResponse(
                content=(
                    '{"run_id":"run-1","overall_summary":"初稿",'
                    '"selected_finding_ids":["merged-missing-method"]}'
                ),
                finish_reason="stop",
            ),
            ModelResponse(
                content=(
                    '{"run_id":"run-1","overall_summary":"修复后",'
                    '"selected_finding_ids":["methods-reviewer:missing-method"]}'
                ),
                finish_reason="stop",
            ),
        ]
    )

    result = await run_meta_reviewer(
        run_id="run-1",
        model=model,
        rubric=RubricProfile(rubric_id="unscored", version="1", title="Unscored"),
        results=[source_result],
        audit=AuditReport(),
        max_repairs=1,
    )

    assert result.findings == [source_finding]
    assert [request.max_output_tokens for request in model.requests] == [8192, 8192]
    repair_instruction = model.requests[1].messages[-1].content or ""
    assert "merged-missing-method" in repair_instruction
    assert "methods-reviewer:missing-method" in repair_instruction


@pytest.mark.asyncio
async def test_meta_reviewer_rejects_duplicate_source_finding_ids_before_model_call() -> None:
    source_result, source_finding = _source_result()
    duplicate = source_finding.model_copy(update={"reviewer_id": "second-reviewer"})
    second_result = ReviewerResult(
        reviewer_id="second-reviewer",
        summary="Duplicate fixture.",
        findings=[duplicate],
    )
    model = RecordingModel([])

    with pytest.raises(ValueError, match="must be globally unique"):
        await run_meta_reviewer(
            run_id="run-1",
            model=model,
            rubric=RubricProfile(rubric_id="unscored", version="1", title="Unscored"),
            results=[source_result, second_result],
            audit=AuditReport(),
            max_repairs=1,
        )

    assert model.requests == []
