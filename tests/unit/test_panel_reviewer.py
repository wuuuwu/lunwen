from __future__ import annotations

import json
from collections import deque

import pytest

from paper_reviewer.agents.loop import InvalidAgentOutput
from paper_reviewer.agents.panel_reviewer import run_panel_reviewer
from paper_reviewer.config import ReviewerProfile
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceKind, EvidenceRef
from paper_reviewer.domain.review import ReviewFinding, Severity
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.ports.model import ModelRequest, ModelResponse


class RecordingModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.popleft()


def _opinion(*, verdict: str, finding_ids: list[str]) -> ModelResponse:
    return ModelResponse(
        content=json.dumps(
            {
                "expert_id": "expert-1",
                "round": "initial",
                "verdict": verdict,
                "rationale": "Independent full-paper assessment.",
                "finding_ids": finding_ids,
            }
        )
    )


def _finding(block: DocumentBlock, *, severity: Severity = Severity.MAJOR) -> ReviewFinding:
    return ReviewFinding(
        finding_id="professional-reviewer-major-1",
        reviewer_id="professional-reviewer",
        dimension_id="professional-ability",
        severity=severity,
        confidence=0.8,
        claim="The analysis does not support the conclusion.",
        rationale="A decisive analytical step is missing.",
        paper_evidence=[
            EvidenceRef(
                evidence_id=f"paper:{block.block_id}",
                kind=EvidenceKind.PAPER,
                block_id=block.block_id,
                page=1,
            )
        ],
        recommendation="Add the missing analysis.",
    )


async def _run(
    model: RecordingModel, finding: ReviewFinding, block: DocumentBlock
) -> object:
    return await run_panel_reviewer(
        run_id="run-panel",
        model=model,
        expert=ReviewerProfile(
            reviewer_id="expert-1",
            title="Independent expert",
            description="Review the complete thesis.",
            allowed_tools=[],
            max_model_turns=1,
            max_tool_calls=0,
        ),
        round="initial",
        rubric=RubricProfile(
            rubric_id="zhejiang-undergraduate",
            version="0.1-experimental",
            title="Zhejiang undergraduate thesis diagnostic rubric",
        ),
        document=DocumentInfo(
            document_id="doc",
            source_path="paper.pdf",
            sha256="a" * 64,
            title="Paper",
            page_count=1,
        ),
        blocks=[block],
        evidence=[],
        findings=[finding],
        discipline_name="Computer Science",
        discipline_profile="Graduates apply computing knowledge to solve problems.",
        max_repairs=0,
    )


@pytest.mark.asyncio
async def test_panel_unqualified_requires_existing_major_finding_with_paper_evidence() -> None:
    source_block = DocumentBlock.create(document_id="doc", page=1, text="Source")
    model = RecordingModel(
        [_opinion(verdict="unqualified", finding_ids=["professional-reviewer-major-1"])]
    )

    result = await _run(model, _finding(source_block), source_block)

    assert result.verdict.value == "unqualified"  # type: ignore[union-attr]
    assert model.requests[0].max_output_tokens == 393_216
    payload = json.loads(model.requests[0].messages[1].content or "{}")
    assert "expert_opinions" not in payload
    assert payload["finding_evidence_blocks"][0]["block_id"] == source_block.block_id


@pytest.mark.asyncio
async def test_panel_rejects_unqualified_vote_based_on_minor_finding() -> None:
    source_block = DocumentBlock.create(document_id="doc", page=1, text="Source")
    model = RecordingModel(
        [_opinion(verdict="unqualified", finding_ids=["professional-reviewer-major-1"])]
    )

    with pytest.raises(InvalidAgentOutput, match="major or critical"):
        await _run(model, _finding(source_block, severity=Severity.MINOR), source_block)


@pytest.mark.asyncio
async def test_panel_accepts_unable_to_assess_without_findings() -> None:
    source_block = DocumentBlock.create(document_id="doc", page=1, text="Source")
    model = RecordingModel([_opinion(verdict="unable_to_assess", finding_ids=[])])

    result = await _run(model, _finding(source_block), source_block)

    assert result.verdict.value == "unable_to_assess"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_panel_rejects_unknown_finding_id() -> None:
    source_block = DocumentBlock.create(document_id="doc", page=1, text="Source")
    model = RecordingModel([_opinion(verdict="unqualified", finding_ids=["invented"])])

    with pytest.raises(InvalidAgentOutput, match="unknown finding ids"):
        await _run(model, _finding(source_block), source_block)


@pytest.mark.asyncio
async def test_panel_rejects_unqualified_vote_with_unknown_paper_block() -> None:
    source_block = DocumentBlock.create(document_id="doc", page=1, text="Source")
    finding = _finding(source_block)
    finding.paper_evidence[0].block_id = "invented-block"
    model = RecordingModel(
        [_opinion(verdict="unqualified", finding_ids=[finding.finding_id])]
    )

    with pytest.raises(InvalidAgentOutput, match="unknown paper block"):
        await _run(model, finding, source_block)
