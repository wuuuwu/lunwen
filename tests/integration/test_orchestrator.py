from __future__ import annotations

import asyncio
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
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.evidence import EvidenceKind, EvidenceRef
from paper_reviewer.domain.review import ReviewerResult, ReviewFinding, Severity
from paper_reviewer.domain.rubric import RubricProfile
from paper_reviewer.domain.run import RunRecord, RunStatus
from paper_reviewer.ports.document_parser import ParsedDocument
from paper_reviewer.ports.model import ModelRequest, ModelResponse


class FakeParser:
    def parse(self, path: Path) -> ParsedDocument:
        return ParsedDocument(
            info=DocumentInfo(
                document_id="doc",
                source_path=str(path),
                sha256="a" * 64,
                title="A test paper",
                page_count=1,
            ),
            blocks=[
                DocumentBlock.create(
                    document_id="doc",
                    page=1,
                    text="The paper describes a method and a small experiment.",
                )
            ],
        )


class FakeModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if request.trace_id.endswith(":meta"):
            run_id = request.trace_id.removesuffix(":meta")
            return ModelResponse(
                content=json.dumps(
                    {
                        "run_id": run_id,
                        "overall_summary": "A structured unscored review.",
                        "selected_finding_ids": [],
                        "disagreements": [],
                        "human_checks": [],
                    }
                )
            )
        reviewer_id = request.trace_id.rsplit(":", 1)[-1]
        return ModelResponse(
            content=json.dumps(
                {
                    "reviewer_id": reviewer_id,
                    "summary": "No evidence-grounded major issue in the fixture.",
                    "findings": [],
                    "dimension_scores": {},
                    "limitations": ["Fixture response"],
                }
            )
        )


class LegacyReferenceRepairModel:
    def __init__(self, block_id: str) -> None:
        self.block_id = block_id
        self.trace_ids: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.trace_ids.append(request.trace_id)
        if request.trace_id.endswith(":meta"):
            return ModelResponse(
                content=json.dumps(
                    {
                        "run_id": request.trace_id.removesuffix(":meta"),
                        "overall_summary": "Recovered review.",
                        "selected_finding_ids": ["novelty-reviewer-003"],
                    }
                )
            )
        return ModelResponse(
            content=json.dumps(
                {
                    "reviewer_id": "novelty-reviewer",
                    "summary": "Repaired novelty review.",
                    "findings": [
                        {
                            "finding_id": "novelty-reviewer-003",
                            "reviewer_id": "novelty-reviewer",
                            "dimension_id": "novelty",
                            "severity": "major",
                            "confidence": 0.9,
                            "claim": "The model tried to rewrite this claim.",
                            "rationale": "The model tried to rewrite this rationale.",
                            "paper_evidence": [
                                {
                                    "evidence_id": f"paper:{self.block_id}",
                                    "kind": "paper",
                                    "block_id": self.block_id,
                                    "page": 1,
                                }
                            ],
                            "external_evidence": [],
                            "recommendation": "Add a comparison.",
                        }
                    ],
                    "dimension_scores": {},
                    "limitations": [],
                }
            )
        )


class PartialFailureModel(FakeModel):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False
        self.trace_ids: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.trace_ids.append(request.trace_id)
        if request.trace_id.endswith(":reviewer-1") and not self.failed_once:
            await asyncio.sleep(0.02)
            self.failed_once = True
            raise RuntimeError("transient reviewer failure")
        return await super().complete(request)


@pytest.mark.asyncio
async def test_complete_run_and_idempotent_resume(tmp_path: Path) -> None:
    database = tmp_path / "review.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        runs_dir=tmp_path / "runs",
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    model = FakeModel()
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=FakeParser(),
        run_repository=RunRepository(sessions),
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=ReviewRepository(sessions),
    )
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"fixture")
    rubric = RubricProfile(
        rubric_id="unscored", version="1", title="Unscored", scoring_enabled=False
    )
    profile = ReviewProfile(
        profile_id="test",
        version="1",
        reviewers=[
            ReviewerProfile(
                reviewer_id=f"reviewer-{index}",
                title="Reviewer",
                description="Review the fixture.",
                allowed_tools=[],
            )
            for index in range(3)
        ],
    )
    run = await orchestrator.create_and_execute(
        input_path=paper,
        rubric=rubric,
        profile=profile,
        provider="fake",
        model_name="fake",
    )
    assert run.status is RunStatus.REPORTED
    assert (settings.runs_dir / run.run_id / "report.md").is_file()
    request_context = json.loads(
        (settings.runs_dir / run.run_id / "request-context.json").read_text(
            encoding="utf-8"
        )
    )
    assert request_context["external_search"] is True
    meta_payload = json.loads(
        (settings.runs_dir / run.run_id / "meta-review.json").read_text(encoding="utf-8")
    )
    assert meta_payload["findings"] == []
    assert "selected_finding_ids" not in meta_payload
    trace_path = settings.runs_dir / run.run_id / "trace.jsonl"
    assert trace_path.is_file()
    trace = trace_path.read_text(encoding="utf-8")
    assert "model_call_completed" in trace
    assert "report_completed" in trace
    matches = await DocumentRepository(sessions).search(run.run_id, "method experiment")
    assert matches
    assert model.calls == 4

    resumed = await orchestrator.execute(run, rubric=rubric, profile=profile)
    assert resumed.status is RunStatus.REPORTED
    assert model.calls == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_resume_only_invokes_missing_reviewer_results(tmp_path: Path) -> None:
    database = tmp_path / "review.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        runs_dir=tmp_path / "runs",
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    runs = RunRepository(sessions)
    reviews = ReviewRepository(sessions)
    model = PartialFailureModel()
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=FakeParser(),
        run_repository=runs,
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=reviews,
    )
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"fixture")
    rubric = RubricProfile(
        rubric_id="unscored", version="1", title="Unscored", scoring_enabled=False
    )
    profile = ReviewProfile(
        profile_id="test",
        version="1",
        reviewers=[
            ReviewerProfile(
                reviewer_id=f"reviewer-{index}",
                title="Reviewer",
                description="Review the fixture.",
                allowed_tools=[],
            )
            for index in range(3)
        ],
    )

    with pytest.raises(RuntimeError, match="transient reviewer failure"):
        await orchestrator.create_and_execute(
            input_path=paper,
            rubric=rubric,
            profile=profile,
            provider="fake",
            model_name="fake",
        )

    failed_run = (await runs.list())[0]
    saved_ids = {item.reviewer_id for item in await reviews.list_results(failed_run.run_id)}
    assert saved_ids == {"reviewer-0", "reviewer-2"}

    resumed = await orchestrator.execute(failed_run, rubric=rubric, profile=profile)

    assert resumed.status is RunStatus.REPORTED
    assert model.trace_ids.count(f"{failed_run.run_id}:reviewer-0") == 1
    assert model.trace_ids.count(f"{failed_run.run_id}:reviewer-2") == 1
    assert model.trace_ids.count(f"{failed_run.run_id}:reviewer-1") == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_resume_repairs_only_reviewer_with_legacy_unknown_block(tmp_path: Path) -> None:
    database = tmp_path / "review.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
        runs_dir=tmp_path / "runs",
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    runs = RunRepository(sessions)
    documents = DocumentRepository(sessions)
    reviews = ReviewRepository(sessions)
    evidence_repository = EvidenceRepository(sessions)

    block = DocumentBlock.create(document_id="doc", page=1, text="A valid comparison passage.")
    invalid_block_id = "70705e53a223cdac5945709508f08a91"
    run = RunRecord(
        run_id="legacy-run",
        status=RunStatus.RETRYABLE_FAILURE,
        input_path=str(tmp_path / "paper.pdf"),
        input_hash="a" * 64,
        config_hash="b" * 64,
        rubric_id="unscored@1",
        provider="fake",
        model="fake",
        completed_stages=["ingest", "evidence", "reviews"],
        error="unknown paper block",
    )
    await runs.create(run)
    await documents.add_blocks(run.run_id, [block])
    await reviews.save_result(
        run.run_id,
        ReviewerResult(
            reviewer_id="novelty-reviewer",
            summary="Legacy invalid review.",
            findings=[
                ReviewFinding(
                    finding_id="novelty-reviewer-003",
                    reviewer_id="novelty-reviewer",
                    dimension_id="novelty",
                    severity=Severity.MAJOR,
                    confidence=0.9,
                    claim="The novelty claim needs support.",
                    rationale="The comparison is incomplete.",
                    paper_evidence=[
                        EvidenceRef(
                            evidence_id=f"paper:{block.block_id}",
                            kind=EvidenceKind.PAPER,
                            block_id=block.block_id,
                            page=1,
                        ),
                        EvidenceRef(
                            evidence_id=f"paper:{invalid_block_id}",
                            kind=EvidenceKind.PAPER,
                            block_id=invalid_block_id,
                            page=1,
                        )
                    ],
                    recommendation="Add a comparison.",
                )
            ],
        ),
    )
    unaffected_result = ReviewerResult(
        reviewer_id="methods-reviewer",
        summary="Valid methods review.",
        findings=[
            ReviewFinding(
                finding_id="methods-reviewer-001",
                reviewer_id="methods-reviewer",
                dimension_id="methods",
                severity=Severity.MINOR,
                confidence=0.8,
                claim="A valid methods finding.",
                rationale="The valid result must not be rerun.",
                paper_evidence=[
                    EvidenceRef(
                        evidence_id=f"paper:{block.block_id}",
                        kind=EvidenceKind.PAPER,
                        block_id=block.block_id,
                        page=1,
                    )
                ],
                recommendation="Clarify the method.",
            )
        ],
    )
    await reviews.save_result(run.run_id, unaffected_result)
    run_dir = settings.runs_dir / run.run_id
    run_dir.mkdir(parents=True)
    document = DocumentInfo(
        document_id="doc",
        source_path=run.input_path,
        sha256=run.input_hash,
        title="Paper",
        page_count=1,
    )
    (run_dir / "document.json").write_text(document.model_dump_json(), encoding="utf-8")

    rubric = RubricProfile(
        rubric_id="unscored", version="1", title="Unscored", scoring_enabled=False
    )
    profile = ReviewProfile(
        profile_id="test",
        version="1",
        reviewers=[
            ReviewerProfile(
                reviewer_id="novelty-reviewer",
                title="Novelty reviewer",
                description="Review novelty.",
                allowed_tools=[],
                max_model_turns=1,
                max_tool_calls=0,
            ),
            ReviewerProfile(
                reviewer_id="methods-reviewer",
                title="Methods reviewer",
                description="Review methods.",
                allowed_tools=[],
                max_model_turns=1,
                max_tool_calls=0,
            ),
        ],
    )
    model = LegacyReferenceRepairModel(block.block_id)
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=FakeParser(),
        run_repository=runs,
        document_repository=documents,
        evidence_repository=evidence_repository,
        review_repository=reviews,
    )

    recovered = await orchestrator.execute(run, rubric=rubric, profile=profile)

    assert recovered.status is RunStatus.REPORTED
    assert model.trace_ids == ["legacy-run:novelty-reviewer", "legacy-run:meta"]
    stored = await reviews.list_results(run.run_id)
    stored_by_reviewer = {result.reviewer_id: result for result in stored}
    assert stored_by_reviewer["methods-reviewer"] == unaffected_result
    repaired_result = stored_by_reviewer["novelty-reviewer"]
    assert repaired_result.summary == "Legacy invalid review."
    repaired_finding = repaired_result.findings[0]
    assert repaired_finding.claim == "The novelty claim needs support."
    assert [ref.block_id for ref in repaired_finding.paper_evidence] == [block.block_id]
    trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert "review_reference_repair_started" in trace
    assert "review_reference_repair_completed" in trace
    assert (run_dir / "report.md").is_file()
    await engine.dispose()
