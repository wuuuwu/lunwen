from __future__ import annotations

import hashlib
import json

import pytest

from paper_reviewer.agents.reviewer import run_reviewer
from paper_reviewer.config import ReviewerProfile
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceItem
from paper_reviewer.ports.model import ModelRequest, ModelResponse, ToolCall
from paper_reviewer.ports.web_search import WebSearchResult
from paper_reviewer.tools.web_search import WebSearchTools


class FakeWebSearch:
    async def search(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        assert query == "agent harness reference"
        assert limit == 3
        return [
            WebSearchResult(
                title="Agent Harness Reference",
                url="https://example.test/reference",
                snippet="A metadata result used to verify the bibliography.",
                source="fixture",
            )
        ]


class SearchThenReviewModel:
    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="web-1",
                        name="web_search",
                        arguments={"query": "agent harness reference", "limit": 3},
                    )
                ]
            )
        return ModelResponse(
            content=json.dumps(
                {
                    "reviewer_id": "reference-reviewer",
                    "summary": "The bibliography entry was checked online.",
                    "findings": [
                        {
                            "finding_id": "reference-reviewer-001",
                            "reviewer_id": "reference-reviewer",
                            "dimension_id": "citation_norms",
                            "severity": "minor",
                            "confidence": 0.7,
                            "claim": "The entry has a web metadata match.",
                            "rationale": "The search result matches the cited title.",
                            "paper_evidence": [],
                            "external_evidence": [
                                {
                                    "evidence_id": self.evidence_id,
                                    "kind": "external",
                                    "title": "Agent Harness Reference",
                                    "url": "https://example.test/reference",
                                    "level": "C",
                                }
                            ],
                            "recommendation": "Retain the canonical URL.",
                        }
                    ],
                    "dimension_scores": {},
                    "limitations": ["Search snippets are metadata-level evidence."],
                }
            )
        )


@pytest.mark.asyncio
async def test_reviewer_can_cite_evidence_created_by_live_web_search() -> None:
    url = "https://example.test/reference"
    evidence_id = "web:" + hashlib.sha256(url.encode()).hexdigest()[:24]
    evidence: list[EvidenceItem] = []
    model = SearchThenReviewModel(evidence_id)
    block = DocumentBlock.create(document_id="doc", page=1, text="Paper text.")

    result = await run_reviewer(
        run_id="run-web",
        model=model,
        reviewer=ReviewerProfile(
            reviewer_id="reference-reviewer",
            title="Reference reviewer",
            description="Check bibliography references.",
            allowed_tools=["web_search"],
            max_model_turns=2,
            max_tool_calls=2,
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
        evidence=evidence,
        scoring_enabled=False,
        max_repairs=0,
        web_search_tools=WebSearchTools(
            client=FakeWebSearch(),
            run_id="run-web",
            evidence=evidence,
        ),
    )

    assert result.findings[0].external_evidence[0].evidence_id == evidence_id
    assert [item.evidence_id for item in evidence] == [evidence_id]
    assert evidence[0].source_name == "web:fixture"
    assert model.requests[0].tools[0].name == "web_search"
    assert any(message.role == "tool" for message in model.requests[1].messages)
