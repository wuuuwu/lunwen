from __future__ import annotations

import json
from collections import deque

import pytest

from paper_reviewer.agents.loop import InvalidAgentOutput
from paper_reviewer.agents.reviewer import run_reviewer
from paper_reviewer.config import ReviewerProfile
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem, EvidenceKind, EvidenceLevel
from paper_reviewer.domain.review import MetaReview, ReviewerResult
from paper_reviewer.domain.rubric import EvidencePolicy, HardRule, RubricDimension
from paper_reviewer.ports.model import ModelRequest, ModelResponse
from paper_reviewer.validation.audits import audit_meta_review, reviewer_reference_errors


class RecordingModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.popleft()


def _reviewer_response(block_id: str) -> ModelResponse:
    return ModelResponse(
        content=json.dumps(
            {
                "reviewer_id": "novelty-reviewer",
                "summary": "A review.",
                "findings": [
                    {
                        "finding_id": "novelty-reviewer-003",
                        "reviewer_id": "novelty-reviewer",
                        "dimension_id": "novelty",
                        "severity": "major",
                        "confidence": 0.9,
                        "claim": "The novelty claim needs stronger support.",
                        "rationale": "The comparison is incomplete.",
                        "paper_evidence": [
                            {
                                "evidence_id": f"paper:{block_id}",
                                "kind": "paper",
                                "block_id": block_id,
                                "page": 1,
                            }
                        ],
                        "external_evidence": [],
                        "recommendation": "Add a direct comparison.",
                    }
                ],
                "dimension_scores": {},
                "limitations": [],
            }
        )
    )


@pytest.mark.asyncio
async def test_reviewer_repairs_major_finding_that_omits_paper_evidence() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="A valid paper passage.")
    invalid_payload = json.loads(_reviewer_response(block.block_id).content or "{}")
    invalid_payload["findings"][0]["paper_evidence"] = []
    model = RecordingModel(
        [
            ModelResponse(content=json.dumps(invalid_payload)),
            _reviewer_response(block.block_id),
        ]
    )

    result = await run_reviewer(
        run_id="run-1",
        model=model,
        reviewer=ReviewerProfile(
            reviewer_id="novelty-reviewer",
            title="Novelty reviewer",
            description="Review novelty.",
            allowed_tools=[],
            max_model_turns=1,
            max_tool_calls=0,
        ),
        dimensions=[],
        document=DocumentInfo(
            document_id="doc",
            source_path="paper.pdf",
            sha256="a" * 64,
            title="Paper",
            page_count=1,
        ),
        blocks=[block],
        evidence=[],
        scoring_enabled=False,
        max_repairs=1,
    )

    assert result.findings[0].paper_evidence[0].block_id == block.block_id
    repair_instruction = model.requests[1].messages[-1].content or ""
    assert "critical or major finding" in repair_instruction
    assert "novelty-reviewer-003" in repair_instruction
    assert '"paper_evidence": []' in repair_instruction


@pytest.mark.asyncio
async def test_reviewer_repairs_unknown_paper_block_before_returning() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="A valid paper passage.")
    invented_block_id = "70705e53a223cdac5945709508f08a91"
    model = RecordingModel(
        [
            _reviewer_response(invented_block_id),
            _reviewer_response(block.block_id),
        ]
    )

    result = await run_reviewer(
        run_id="run-1",
        model=model,
        reviewer=ReviewerProfile(
            reviewer_id="novelty-reviewer",
            title="Novelty reviewer",
            description="Review novelty.",
            allowed_tools=[],
            max_model_turns=1,
            max_tool_calls=0,
        ),
        dimensions=[],
        document=DocumentInfo(
            document_id="doc",
            source_path="paper.pdf",
            sha256="a" * 64,
            title="Paper",
            page_count=1,
        ),
        blocks=[block],
        evidence=[],
        scoring_enabled=False,
        max_repairs=1,
    )

    assert result.findings[0].paper_evidence[0].block_id == block.block_id
    assert len(model.requests) == 2
    repair_instruction = model.requests[1].messages[-1].content or ""
    assert invented_block_id in repair_instruction
    assert "unknown paper block" in repair_instruction
    assert all(
        invented_block_id not in (message.content or "")
        for message in model.requests[1].messages[:-1]
    )


@pytest.mark.asyncio
async def test_reviewer_uses_provider_maximum_output_limit() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="A valid paper passage.")
    model = RecordingModel([_reviewer_response(block.block_id)])

    result = await run_reviewer(
        run_id="run-1",
        model=model,
        reviewer=ReviewerProfile(
            reviewer_id="novelty-reviewer",
            title="Novelty reviewer",
            description="Review novelty.",
            allowed_tools=[],
            max_model_turns=1,
            max_tool_calls=0,
        ),
        dimensions=[],
        document=DocumentInfo(
            document_id="doc",
            source_path="paper.pdf",
            sha256="a" * 64,
            title="Paper",
            page_count=1,
        ),
        blocks=[block],
        evidence=[],
        scoring_enabled=False,
        max_repairs=1,
    )

    assert result.findings[0].paper_evidence[0].block_id == block.block_id
    assert [request.max_output_tokens for request in model.requests] == [393_216]


@pytest.mark.asyncio
async def test_reference_repair_cannot_drop_the_invalid_candidate_findings() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="A valid paper passage.")
    invented_block_id = "70705e53a223cdac5945709508f08a91"
    model = RecordingModel(
        [
            _reviewer_response(invented_block_id),
            ModelResponse(
                content=json.dumps(
                    {
                        "reviewer_id": "novelty-reviewer",
                        "summary": "Tried to evade repair.",
                        "findings": [],
                        "dimension_scores": {},
                        "limitations": [],
                    }
                )
            ),
        ]
    )

    with pytest.raises(InvalidAgentOutput, match="must preserve the exact finding_id set"):
        await run_reviewer(
            run_id="run-1",
            model=model,
            reviewer=ReviewerProfile(
                reviewer_id="novelty-reviewer",
                title="Novelty reviewer",
                description="Review novelty.",
                allowed_tools=[],
                max_model_turns=1,
                max_tool_calls=0,
            ),
            dimensions=[],
            document=DocumentInfo(
                document_id="doc",
                source_path="paper.pdf",
                sha256="a" * 64,
                title="Paper",
                page_count=1,
            ),
            blocks=[block],
            evidence=[],
            scoring_enabled=False,
            max_repairs=1,
        )


def _external_review_response(evidence_id: str) -> ModelResponse:
    return ModelResponse(
        content=json.dumps(
            {
                "reviewer_id": "novelty-reviewer",
                "summary": "An external-evidence review.",
                "findings": [
                    {
                        "finding_id": "novelty-reviewer-external",
                        "reviewer_id": "novelty-reviewer",
                        "dimension_id": "novelty",
                        "severity": "minor",
                        "confidence": 0.8,
                        "claim": "The related-work comparison is incomplete.",
                        "rationale": "A relevant external source is missing.",
                        "paper_evidence": [],
                        "external_evidence": [
                            {
                                "evidence_id": evidence_id,
                                "kind": "external",
                                "title": "Relevant paper",
                                "level": "A",
                            }
                        ],
                        "recommendation": "Discuss the relevant work.",
                    }
                ],
                "dimension_scores": {},
                "limitations": [],
            }
        )
    )


@pytest.mark.asyncio
async def test_reviewer_repairs_unknown_external_evidence_id() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="A paper passage.")
    evidence = EvidenceItem(
        evidence_id="known-evidence",
        run_id="run-1",
        kind=EvidenceKind.EXTERNAL,
        title="Relevant paper",
        content="Relevant scholarly evidence.",
        source_name="fixture",
        level=EvidenceLevel.FULL_TEXT,
    )
    model = RecordingModel(
        [
            _external_review_response("invented-evidence"),
            _external_review_response(evidence.evidence_id),
        ]
    )

    result = await run_reviewer(
        run_id="run-1",
        model=model,
        reviewer=ReviewerProfile(
            reviewer_id="novelty-reviewer",
            title="Novelty reviewer",
            description="Review novelty.",
            allowed_tools=[],
            max_model_turns=1,
            max_tool_calls=0,
        ),
        dimensions=[],
        document=DocumentInfo(
            document_id="doc",
            source_path="paper.pdf",
            sha256="a" * 64,
            title="Paper",
            page_count=1,
        ),
        blocks=[block],
        evidence=[evidence],
        scoring_enabled=False,
        max_repairs=1,
    )

    assert result.findings[0].external_evidence[0].evidence_id == evidence.evidence_id
    assert "unknown external evidence invented-evidence" in (
        model.requests[1].messages[-1].content or ""
    )


def test_reference_audit_rejects_wrong_kind_in_paper_evidence_list() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="A paper passage.")
    payload = json.loads(_reviewer_response(block.block_id).content or "{}")
    reference = payload["findings"][0]["paper_evidence"][0]
    reference["kind"] = "external"
    reference["title"] = "External source placed in the wrong list"
    result = ReviewerResult.model_validate(payload)

    errors = reviewer_reference_errors(
        result=result,
        block_ids={block.block_id},
        evidence_ids=set(),
    )

    assert any("paper_evidence contains non-paper reference" in error for error in errors)

    meta_audit = audit_meta_review(
        meta=MetaReview(
            run_id="run-1",
            overall_summary="Summary",
            findings=result.findings,
        ),
        source_results=[result],
        blocks=[block],
        evidence=[],
        scoring_enabled=False,
    )
    assert any(
        "meta paper_evidence contains non-paper reference" in error
        for error in meta_audit.errors
    )


def _policy_reviewer_response(
    block_id: str, *, hard_rule_status: str = "suspected"
) -> ModelResponse:
    return ModelResponse(
        content=json.dumps(
            {
                "reviewer_id": "policy-reviewer",
                "summary": "Structured diagnostic review.",
                "findings": [],
                "dimension_scores": {},
                "limitations": [],
                "criterion_assessments": [
                    {
                        "criterion_id": "topic-purpose",
                        "reviewer_id": "policy-reviewer",
                        "rating": 3,
                        "weight": 10,
                        "rationale": "The purpose is explicit.",
                        "paper_evidence": [
                            {
                                "evidence_id": f"paper:{block_id}",
                                "kind": "paper",
                                "block_id": block_id,
                                "page": 1,
                            }
                        ],
                        "external_evidence": [],
                        "confidence": 0.8,
                    }
                ],
                "hard_rule_assessments": [
                    {
                        "rule_id": "integrity",
                        "reviewer_id": "policy-reviewer",
                        "status": hard_rule_status,
                        "rationale": "A passage requires human verification.",
                        "paper_evidence": [
                            {
                                "evidence_id": f"paper:{block_id}",
                                "kind": "paper",
                                "block_id": block_id,
                                "page": 1,
                            }
                        ],
                        "external_evidence": [],
                    }
                ],
            }
        )
    )


@pytest.mark.asyncio
async def test_policy_reviewer_returns_criterion_and_hard_rule_assessments() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="Research purpose.")
    model = RecordingModel([_policy_reviewer_response(block.block_id)])

    result = await run_reviewer(
        run_id="run-policy",
        model=model,
        reviewer=ReviewerProfile(
            reviewer_id="policy-reviewer",
            title="Policy reviewer",
            description="Review purpose and integrity.",
            allowed_tools=[],
            max_model_turns=1,
            max_tool_calls=0,
        ),
        dimensions=[
            RubricDimension(
                dimension_id="topic-purpose",
                title="Topic purpose",
                description="Assess the research purpose.",
                weight=10,
                minimum_score=0,
                maximum_score=4,
                checks=["Purpose is explicit"],
                evidence_policy=EvidencePolicy(paper_evidence_required=True),
            )
        ],
        hard_rules=[
            HardRule(
                rule_id="integrity",
                description="Academic integrity",
                outcome="human_confirmation",
            )
        ],
        document=DocumentInfo(
            document_id="doc",
            source_path="paper.pdf",
            sha256="a" * 64,
            title="Paper",
            page_count=1,
        ),
        blocks=[block],
        evidence=[],
        scoring_enabled=True,
        max_repairs=0,
        discipline_name="Computer Science",
    )

    assert result.criterion_assessments[0].rating == 3
    assert result.hard_rule_assessments[0].status.value == "suspected"
    assert model.requests[0].max_output_tokens == 393_216


@pytest.mark.asyncio
async def test_policy_reviewer_repairs_mismatched_criterion_quote() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="Research purpose.")
    invalid = _policy_reviewer_response(block.block_id)
    invalid_payload = json.loads(invalid.content or "{}")
    invalid_payload["criterion_assessments"][0]["paper_evidence"][0]["quote"] = (
        "Research…"
    )
    valid = _policy_reviewer_response(block.block_id)
    valid_payload = json.loads(valid.content or "{}")
    valid_payload["criterion_assessments"][0]["paper_evidence"][0]["quote"] = (
        "Research purpose."
    )
    model = RecordingModel(
        [
            ModelResponse(content=json.dumps(invalid_payload)),
            ModelResponse(content=json.dumps(valid_payload)),
        ]
    )

    result = await run_reviewer(
        run_id="run-policy",
        model=model,
        reviewer=ReviewerProfile(
            reviewer_id="policy-reviewer",
            title="Policy reviewer",
            description="Review purpose and integrity.",
            allowed_tools=[],
            max_model_turns=1,
            max_tool_calls=0,
        ),
        dimensions=[
            RubricDimension(
                dimension_id="topic-purpose",
                title="Topic purpose",
                description="Assess the research purpose.",
                weight=10,
                minimum_score=0,
                maximum_score=4,
                checks=["Purpose is explicit"],
                evidence_policy=EvidencePolicy(paper_evidence_required=True),
            )
        ],
        hard_rules=[
            HardRule(
                rule_id="integrity",
                description="Academic integrity",
                outcome="human_confirmation",
            )
        ],
        document=DocumentInfo(
            document_id="doc",
            source_path="paper.pdf",
            sha256="a" * 64,
            title="Paper",
            page_count=1,
        ),
        blocks=[block],
        evidence=[],
        scoring_enabled=True,
        max_repairs=1,
        discipline_name="Computer Science",
    )

    assert result.criterion_assessments[0].paper_evidence[0].quote == "Research purpose."
    assert len(model.requests) == 2
    repair_message = model.requests[1].messages[-1].content or ""
    assert "criterion topic-purpose" in repair_message
    assert "quote does not match its block" in repair_message


@pytest.mark.asyncio
async def test_policy_reviewer_cannot_confirm_hard_rule() -> None:
    block = DocumentBlock.create(document_id="doc", page=1, text="Research purpose.")
    model = RecordingModel(
        [_policy_reviewer_response(block.block_id, hard_rule_status="confirmed")]
    )

    with pytest.raises(InvalidAgentOutput, match="AI reviewer cannot set human-confirmed"):
        await run_reviewer(
            run_id="run-policy",
            model=model,
            reviewer=ReviewerProfile(
                reviewer_id="policy-reviewer",
                title="Policy reviewer",
                description="Review purpose and integrity.",
                allowed_tools=[],
                max_model_turns=1,
                max_tool_calls=0,
            ),
            dimensions=[
                RubricDimension(
                    dimension_id="topic-purpose",
                    title="Topic purpose",
                    description="Assess the research purpose.",
                    weight=10,
                    minimum_score=0,
                    maximum_score=4,
                    checks=["Purpose is explicit"],
                )
            ],
            hard_rules=[
                HardRule(
                    rule_id="integrity",
                    description="Academic integrity",
                    outcome="human_confirmation",
                )
            ],
            document=DocumentInfo(
                document_id="doc",
                source_path="paper.pdf",
                sha256="a" * 64,
                title="Paper",
                page_count=1,
            ),
            blocks=[block],
            evidence=[],
            scoring_enabled=True,
            max_repairs=0,
        )
