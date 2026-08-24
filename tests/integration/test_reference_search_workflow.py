from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_reviewer.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    initialize_database,
)
from paper_reviewer.adapters.persistence.repositories import (
    DocumentRepository,
    EvidenceRepository,
    ReviewRepository,
    RunRepository,
)
from paper_reviewer.application.orchestrator import ReviewOrchestrator
from paper_reviewer.config import ReviewerProfile, ReviewProfile, Settings
from paper_reviewer.domain.document import BlockType, DocumentBlock, DocumentInfo
from paper_reviewer.domain.reference import ReferenceCheckReport
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunStatus
from paper_reviewer.ports.document_parser import ParsedDocument
from paper_reviewer.ports.model import ModelRequest, ModelResponse
from paper_reviewer.ports.web_search import WebSearchResult


class ReferenceParser:
    def parse(self, path: Path) -> ParsedDocument:
        return ParsedDocument(
            info=DocumentInfo(
                document_id="doc",
                source_path=str(path),
                sha256="a" * 64,
                title="Reference verification fixture",
                page_count=2,
            ),
            blocks=[
                DocumentBlock.create(
                    document_id="doc",
                    page=1,
                    text="A short paper with a bibliography.",
                ),
                DocumentBlock.create(
                    document_id="doc",
                    page=2,
                    text="[1] Smith J. Reliable Agent Evaluation. Journal, 2022.",
                    block_type=BlockType.REFERENCE,
                    section_path=["References"],
                ),
            ],
        )


class EmptyReviewModel:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.trace_id.endswith(":meta"):
            return ModelResponse(
                content=json.dumps(
                    {
                        "run_id": request.trace_id.removesuffix(":meta"),
                        "overall_summary": "Reference verification completed.",
                        "selected_finding_ids": [],
                    }
                )
            )
        return ModelResponse(
            content=json.dumps(
                {
                    "reviewer_id": "reference-reviewer",
                    "summary": "No review finding.",
                    "findings": [],
                    "dimension_scores": {},
                    "limitations": [],
                }
            )
        )


class FixtureWebSearch:
    def __init__(self, *, matched: bool) -> None:
        self.matched = matched
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5) -> list[WebSearchResult]:
        self.queries.append(query)
        if not self.matched:
            return []
        return [
            WebSearchResult(
                title="Reliable Agent Evaluation",
                url="https://example.test/reliable-agent-evaluation",
                snippet="Journal metadata for the 2022 paper.",
                source="fixture",
                metadata={"year": 2022},
            )
        ]


async def _run_fixture(tmp_path: Path, *, matched: bool) -> tuple[Path, FixtureWebSearch]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}",
        runs_dir=tmp_path / "runs",
        max_reference_checks=10,
        reference_check_concurrency=1,
        reference_search_results=2,
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    web_search = FixtureWebSearch(matched=matched)
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=EmptyReviewModel(),
        parser=ReferenceParser(),
        run_repository=RunRepository(sessions),
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=ReviewRepository(sessions),
        web_search_client=web_search,
    )
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"fixture")
    run = await orchestrator.create_and_execute(
        input_path=paper,
        rubric=RubricProfile(
            rubric_id="unscored",
            version="1",
            title="Unscored",
            scoring_enabled=False,
        ),
        profile=ReviewProfile(
            profile_id="reference",
            version="1",
            reviewers=[
                ReviewerProfile(
                    reviewer_id="reference-reviewer",
                    title="Reference reviewer",
                    description="Review references.",
                    allowed_tools=[],
                )
            ],
        ),
        provider="fake",
        model_name="fake",
    )
    assert run.status is RunStatus.REPORTED
    await engine.dispose()
    return settings.runs_dir / run.run_id, web_search


@pytest.mark.asyncio
async def test_verified_reference_is_frozen_as_citable_evidence(tmp_path: Path) -> None:
    run_dir, web_search = await _run_fixture(tmp_path, matched=True)

    report = ReferenceCheckReport.model_validate_json(
        (run_dir / "reference-checks.json").read_text(encoding="utf-8")
    )
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8")

    assert report.verified_count == 1
    assert report.unresolved_count == 0
    assert evidence[0]["metadata"]["verification_status"] == "verified"
    assert evidence[0]["metadata"]["reference_id"] == report.checks[0].entry.reference_id
    assert web_search.queries == [report.checks[0].entry.text]
    assert "reference_check_started" in trace
    assert "reference_check_completed" in trace
    assert "建议人工核对" not in (run_dir / "report.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_unresolved_reference_warns_for_manual_check_without_failing_run(
    tmp_path: Path,
) -> None:
    run_dir, _ = await _run_fixture(tmp_path, matched=False)

    report = ReferenceCheckReport.model_validate_json(
        (run_dir / "reference-checks.json").read_text(encoding="utf-8")
    )
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")

    assert report.unresolved_count == 1
    assert "建议人工核对" in markdown
    assert json.loads((run_dir / "evidence.json").read_text(encoding="utf-8")) == []
