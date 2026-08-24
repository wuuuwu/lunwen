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
from paper_reviewer.domain.review import HumanRuleDecision
from paper_reviewer.domain.run import RunStatus
from paper_reviewer.ports.document_parser import ParsedDocument
from paper_reviewer.ports.model import ModelRequest, ModelResponse


class ZhejiangFixtureParser:
    def parse(self, path: Path) -> ParsedDocument:
        return ParsedDocument(
            info=DocumentInfo(
                document_id="zhejiang-fixture",
                source_path=str(path),
                sha256="a" * 64,
                title="本科毕业论文测试样本",
                page_count=1,
            ),
            blocks=[
                DocumentBlock.create(
                    document_id="zhejiang-fixture",
                    page=1,
                    text=(
                        "论文研究目标明确，结构完整，采用合理方法分析专业问题，"
                        "并规范列出参考文献。"
                    ),
                )
            ],
        )


class ZhejiangFixtureModel:
    dimensions: ClassVar[dict[str, list[tuple[str, int]]]] = {
        "topic-specialist": [("topic_purpose", 10), ("research_significance", 10)],
        "logic-specialist": [("hierarchy_system", 10), ("logical_structure", 10)],
        "professional-specialist": [
            ("knowledge_application", 10),
            ("problem_solving", 20),
            ("innovation", 10),
        ],
        "academic-norms-specialist": [("writing_norms", 10), ("citation_norms", 10)],
        "compliance-integrity-specialist": [],
    }

    def __init__(self, *, suspect_integrity: bool = False) -> None:
        self.trace_ids: list[str] = []
        self.suspect_integrity = suspect_integrity

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.trace_ids.append(request.trace_id)
        if request.trace_id.endswith(":meta"):
            return ModelResponse(
                content=json.dumps(
                    {
                        "run_id": request.trace_id.removesuffix(":meta"),
                        "overall_summary": "九项诊断已完成，未发现足以触发风险的重大问题。",
                        "selected_finding_ids": [],
                        "disagreements": [],
                        "human_checks": [],
                    },
                    ensure_ascii=False,
                )
            )
        if ":panel:" in request.trace_id:
            _prefix, _panel, round_name, expert_id = request.trace_id.rsplit(":", 3)
            return ModelResponse(
                content=json.dumps(
                    {
                        "expert_id": expert_id,
                        "round": round_name,
                        "verdict": "qualified",
                        "rationale": "未发现已有重大问题足以支持不合格意见。",
                        "finding_ids": [],
                        "confidence": 0.5,
                    },
                    ensure_ascii=False,
                )
            )

        reviewer_id = request.trace_id.rsplit(":", 1)[-1]
        user_payload = json.loads(request.messages[-1].content or "{}")
        overview = user_payload["paper_overview"]
        block_id = overview[0]["block_id"]
        evidence = [
            {
                "evidence_id": f"paper:{block_id}",
                "kind": "paper",
                "block_id": block_id,
                "page": 1,
                "quote": "论文研究目标明确",
            }
        ]
        hard_rules = []
        if reviewer_id == "compliance-integrity-specialist":
            hard_rules = [
                {
                    "rule_id": rule_id,
                    "reviewer_id": reviewer_id,
                    "status": (
                        "suspected"
                        if self.suspect_integrity and rule_id == "academic_integrity"
                        else "not_detected"
                    ),
                    "rationale": (
                        "引用来源存在需由教师线下核对的诚信嫌疑。"
                        if self.suspect_integrity and rule_id == "academic_integrity"
                        else "未在现有材料中发现可验证嫌疑。"
                    ),
                    "paper_evidence": (
                        evidence
                        if self.suspect_integrity and rule_id == "academic_integrity"
                        else []
                    ),
                    "external_evidence": [],
                }
                for rule_id in ("political_direction", "academic_integrity")
            ]
        return ModelResponse(
            content=json.dumps(
                {
                    "reviewer_id": reviewer_id,
                    "summary": "该专项基本达到本科要求。",
                    "findings": [],
                    "dimension_scores": {},
                    "criterion_assessments": [
                        {
                            "criterion_id": dimension_id,
                            "reviewer_id": reviewer_id,
                            "rating": 2,
                            "weight": weight,
                            "rationale": "论文证据表明基本达到本科最低要求。",
                            "paper_evidence": evidence,
                            "external_evidence": [],
                            "confidence": 0.5,
                        }
                        for dimension_id, weight in self.dimensions[reviewer_id]
                    ],
                    "hard_rule_assessments": hard_rules,
                    "limitations": [],
                },
                ensure_ascii=False,
            )
        )


@pytest.mark.asyncio
async def test_bundled_zhejiang_v2_completes_full_dual_advisory_flow(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}",
        runs_dir=tmp_path / "runs",
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    reviews = ReviewRepository(sessions)
    model = ZhejiangFixtureModel()
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=ZhejiangFixtureParser(),
        run_repository=RunRepository(sessions),
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=reviews,
    )
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"fixture")
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    profile = load_review_profile(
        Path("configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml")
    )
    panel_profile = load_review_profile(
        Path("configs/review_profiles/zhejiang_independent_panel_v1.yaml")
    )

    run = await orchestrator.create_and_execute(
        input_path=paper,
        rubric=rubric,
        profile=profile,
        panel_profile=panel_profile,
        provider="fake",
        model_name="fake",
        discipline_name="计算机科学与技术",
        cloud_processing_authorized=True,
        contains_classified_material=False,
    )

    assert run.status is RunStatus.REPORTED
    run_dir = settings.runs_dir / run.run_id
    evaluation = json.loads((run_dir / "evaluation-report.json").read_text(encoding="utf-8"))
    assert evaluation["diagnostic_score"]["total_score"] == 50
    assert evaluation["panel_decision"]["outcome"] == "risk_not_triggered"
    assert len(evaluation["expert_opinions"]) == 3
    report_json = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report_json["panel_decision"]["outcome"] == "risk_not_triggered"
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "本结果不是浙江省教育厅正式抽检结论" in markdown
    assert "学术不端检测报告未由系统自动读取" in markdown
    assert len(await reviews.list_results(run.run_id)) == 5

    calls = len(model.trace_ids)
    resumed = await orchestrator.execute(
        run,
        rubric=rubric,
        profile=profile,
        panel_profile=panel_profile,
    )
    assert resumed.status is RunStatus.REPORTED
    assert len(model.trace_ids) == calls
    await engine.dispose()


@pytest.mark.asyncio
async def test_confirmed_hard_rule_pauses_then_overrides_panel_votes(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}",
        runs_dir=tmp_path / "runs",
    )
    engine = create_engine(settings.database_url)
    await initialize_database(engine)
    sessions = create_session_factory(engine)
    reviews = ReviewRepository(sessions)
    model = ZhejiangFixtureModel(suspect_integrity=True)
    orchestrator = ReviewOrchestrator(
        settings=settings,
        model=model,
        parser=ZhejiangFixtureParser(),
        run_repository=RunRepository(sessions),
        document_repository=DocumentRepository(sessions),
        evidence_repository=EvidenceRepository(sessions),
        review_repository=reviews,
    )
    paper = tmp_path / "paper.pdf"
    paper.write_bytes(b"fixture")
    rubric = load_rubric(Path("configs/rubrics/zhejiang_undergraduate_thesis_v2.yaml"))
    profile = load_review_profile(
        Path("configs/review_profiles/zhejiang_undergraduate_specialists_v1.yaml")
    )
    panel_profile = load_review_profile(
        Path("configs/review_profiles/zhejiang_independent_panel_v1.yaml")
    )

    run = await orchestrator.create_and_execute(
        input_path=paper,
        rubric=rubric,
        profile=profile,
        panel_profile=panel_profile,
        provider="fake",
        model_name="fake",
        discipline_name="计算机科学与技术",
        cloud_processing_authorized=True,
    )
    assert run.status is RunStatus.AWAITING_HARD_RULE_CONFIRMATION
    assert not any(":panel:" in trace_id for trace_id in model.trace_ids)

    decision = HumanRuleDecision(
        rule_id="academic_integrity",
        decision="confirmed",
        reviewer="教师甲",
        rationale="已在线下核查原文和学校检测结论，确认该嫌疑成立。",
        decided_at="2026-08-23T10:00:00+08:00",
    )
    await reviews.hard_rules.save_human_rule_decision(run.run_id, decision)
    resumed = await orchestrator.execute(
        run,
        rubric=rubric,
        profile=profile,
        panel_profile=panel_profile,
    )

    assert resumed.status is RunStatus.REPORTED
    assert not any(":panel:" in trace_id for trace_id in model.trace_ids)
    evaluation = json.loads(
        (settings.runs_dir / run.run_id / "evaluation-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert evaluation["diagnostic_score"]["total_score"] == 50
    assert evaluation["panel_decision"]["outcome"] == "risk_triggered"
    assert evaluation["panel_decision"]["decisive_rule_ids"] == ["academic_integrity"]
    await engine.dispose()
