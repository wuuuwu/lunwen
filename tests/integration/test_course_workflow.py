from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

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
from paper_reviewer.config import Settings, load_review_profile, load_rubric
from paper_reviewer.domain.document import DocumentBlock, DocumentInfo
from paper_reviewer.domain.run import RunStatus
from paper_reviewer.ports.document_parser import ParsedDocument
from paper_reviewer.ports.model import ModelRequest, ModelResponse, ToolCall

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COURSE_RUBRIC = PROJECT_ROOT / "configs" / "rubrics" / "course_paper_v1.yaml"
COURSE_PROFILE = PROJECT_ROOT / "configs" / "review_profiles" / "course_paper_reviewers_v1.yaml"
FULLWIDTH_STUDENT_ID = "\uff12\uff10\uff12\uff16\uff10\uff10\uff10\uff11"


class CourseFixtureParser:
    """A parser fixture with a removable identity cover and anonymous content."""

    def __init__(self) -> None:
        self.cover = DocumentBlock.create(
            document_id="course-document",
            page=1,
            text="姓名：张三 学号：20260001 专业：历史学 题目：课程论文中的公共治理分析",
        )
        self.body = DocumentBlock.create(
            document_id="course-document",
            page=2,
            text=(
                "本文围绕课程要求分析公共治理案例，说明核心概念与分析方法，"
                "并依据正文证据形成结论。参考文献：课程资料，2025。"
                f"封面登记学号{FULLWIDTH_STUDENT_ID}不得影响匿名评分。"
            ),
        )
        self.late_cover = DocumentBlock.create(
            document_id="course-document",
            page=4,
            text="姓名：李四 学号：20269999 专业：经济学",
        )

    def parse(self, path: Path) -> ParsedDocument:
        return ParsedDocument(
            info=DocumentInfo(
                document_id="course-document",
                source_path=str(path),
                sha256="a" * 64,
                title="张三_20260001_历史学_课程论文中的公共治理分析",
                embedded_title="示例学院",
                visible_title="课程论文中的公共治理分析",
                visible_title_page=2,
                visible_title_block_ids=[self.body.block_id],
                page_count=4,
            ),
            blocks=[self.cover, self.body, self.late_cover],
        )


class BlindCourseModel:
    """Return one metadata extraction, three reviewer results and one meta result."""

    reviewer_dimensions: ClassVar[dict[str, tuple[str, ...]]] = {
        "course-requirements-reviewer": (
            "task_completion",
            "course_knowledge_application",
        ),
        "argumentation-reviewer": ("argument_evidence", "structure_logic"),
        "writing-norms-reviewer": ("writing_expression", "citation_format"),
    }

    def __init__(self, cover_block_id: str) -> None:
        self.cover_block_id = cover_block_id
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if request.trace_id.endswith(":submission-metadata"):
            def field(value: str) -> dict[str, object]:
                return {
                    "value": value,
                    "block_id": self.cover_block_id,
                    "page": 1,
                    "quote": f"{value}",
                    "confidence": 0.99,
                }

            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="metadata-call",
                        name="submit_submission_metadata",
                        arguments={
                            "student_name": field("张三"),
                            "student_id": field("20260001"),
                            "major": field("历史学"),
                            "paper_title": field("课程论文中的公共治理分析"),
                        },
                    )
                ]
            )

        if request.trace_id.endswith(":meta"):
            run_id = request.trace_id.removesuffix(":meta")
            return ModelResponse(
                content=json.dumps(
                    {
                        "run_id": run_id,
                        "overall_summary": "课程论文整体完成了课程要求，论证和表达基本清楚。",
                        "selected_finding_ids": [],
                        "disagreements": [],
                        "human_checks": [],
                    },
                    ensure_ascii=False,
                )
            )

        reviewer_id = request.trace_id.rsplit(":", 1)[-1]
        dimensions = self.reviewer_dimensions[reviewer_id]
        return ModelResponse(
            content=json.dumps(
                {
                    "reviewer_id": reviewer_id,
                    "summary": "该部分课程论文内容有明确证据支持。",
                    "findings": [],
                    "dimension_scores": {
                        dimension_id: {
                            "score": 80,
                            "explanation": "正文证据表明达到课程论文要求。",
                        }
                        for dimension_id in dimensions
                    },
                    "limitations": [],
                },
                ensure_ascii=False,
            )
        )


def _request_text(request: ModelRequest) -> str:
    return request.model_dump_json()


@pytest.mark.asyncio
async def test_course_run_extracts_once_blinds_reviewers_and_writes_course_artifacts(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}",
        runs_dir=tmp_path / "runs",
        reviewer_concurrency=3,
        max_reference_checks=1,
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    parser = CourseFixtureParser()
    model = BlindCourseModel(parser.cover.block_id)
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=parser,
        run_repository=RunRepository(sessions),
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=ReviewRepository(sessions),
    )
    paper = tmp_path / "张三_20260001_历史学_课程论文中的公共治理分析.pdf"
    paper.write_bytes(b"fixture")
    rubric = load_rubric(COURSE_RUBRIC)
    profile = load_review_profile(COURSE_PROFILE)

    run = await orchestrator.create_and_execute(
        input_path=paper,
        rubric=rubric,
        profile=profile,
        provider="fake",
        model_name="fixture-model",
        # Course mode must not require or retain discipline context.
        discipline_name="不应进入课程评测",
        external_search=False,
    )

    assert run.status is RunStatus.REPORTED
    assert [request.trace_id for request in model.requests] == [
        f"{run.run_id}:submission-metadata",
        f"{run.run_id}:course-requirements-reviewer",
        f"{run.run_id}:argumentation-reviewer",
        f"{run.run_id}:writing-norms-reviewer",
        f"{run.run_id}:meta",
    ]

    metadata_path = settings.runs_dir / run.run_id / "submission-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["student_name"] == "张三"
    assert metadata["student_id"] == "20260001"
    assert metadata["major"] == "历史学"

    reviewer_requests = model.requests[1:4]
    downstream_text = "\n".join(
        _request_text(request) for request in (*reviewer_requests, model.requests[4])
    )
    downstream_message_text = "\n".join(
        message.content or ""
        for request in (*reviewer_requests, model.requests[4])
        for message in request.messages
    )
    for secret in (
        "张三",
        "20260001",
        FULLWIDTH_STUDENT_ID,
        "历史学",
        "李四",
        "20269999",
        "经济学",
        str(paper),
        paper.name,
        "姓名：",
        "学号：",
        "示例学院",
        '"visible_title": "课程论文中的公共治理分析"',
        '"visible_title_block_ids"',
    ):
        assert secret not in downstream_text
    assert '"discipline_name": ""' in downstream_message_text
    assert '"source_path": "course-paper.pdf"' in downstream_message_text

    run_dir = settings.runs_dir / run.run_id
    for prohibited in (
        "hard-rule-assessments.json",
        "human-rule-decisions.json",
        "panel-profile.json",
        "expert-opinions.json",
        "expert-panel-decision.json",
        "panel-decision.json",
        "diagnostic-score.json",
        "evaluation-report.json",
    ):
        assert not (run_dir / prohibited).exists(), prohibited
    assert (run_dir / "submission-metadata.json").is_file()
    assert (run_dir / "dimension-scores.json").is_file()
    assert (run_dir / "meta-review.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "report.json").is_file()

    await engine.dispose()


@pytest.mark.asyncio
async def test_course_resume_uses_metadata_checkpoint_without_repeating_any_model_call(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}",
        runs_dir=tmp_path / "runs",
        reviewer_concurrency=3,
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    parser = CourseFixtureParser()
    model = BlindCourseModel(parser.cover.block_id)
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=parser,
        run_repository=RunRepository(sessions),
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=ReviewRepository(sessions),
    )
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"fixture")
    rubric = load_rubric(COURSE_RUBRIC)
    profile = load_review_profile(COURSE_PROFILE)

    run = await orchestrator.create_and_execute(
        input_path=paper,
        rubric=rubric,
        profile=profile,
        provider="fake",
        model_name="fixture-model",
        external_search=False,
    )
    initial_calls = len(model.requests)
    resumed = await orchestrator.execute(run, rubric=rubric, profile=profile)

    assert resumed.status is RunStatus.REPORTED
    assert len(model.requests) == initial_calls == 5
    assert (
        sum(
            request.trace_id.endswith(":submission-metadata")
            for request in model.requests
        )
        == 1
    )
    assert (settings.runs_dir / run.run_id / "submission-metadata.json").is_file()
    await engine.dispose()
